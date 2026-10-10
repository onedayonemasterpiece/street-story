from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=45.0, pool=10.0)
POLL_SECONDS = 5.0
RESEARCH_TIMEOUT_SECONDS = 12 * 60
VISUAL_TIMEOUT_SECONDS = 12 * 60
SOCIAL_TIMEOUT_SECONDS = 6 * 60
OWNER_PROMPT_SHA256 = "92496e7fd70419af40312865f486907fecea9ab84fdb35edc0fbef427faec424"
FIXTURE_META_PATH = Path(__file__).with_name("golden_fixture.json")
WIKIMEDIA_USER_AGENT = (
    "StreetStoryLiveE2EBot/5.0 (https://github.com/onedayonemasterpiece/street-story; public golden fixture)"
)
INITIAL_VOICE_TEXTS = (
    "Я сфотографировал Бранденбургские ворота в Калининграде.",
    "Хочу понять историю этих ворот и когда появился их нынешний облик.",
    "Найди проверяемые городские факты и покажи реальные источники, без легенд без подтверждения.",
)
REFINEMENT_VOICE_TEXT = (
    "Добавь только то, что подтверждено источниками, и сохрани мой выбор фактов. "
    "Мне особенно интересна роль этих ворот в городской среде."
)
FILLER_MARKERS = ("э-э", "эээ", "а-а", "я я", "еще еще")


class LiveE2EError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Diagnostics:
    output_path: Path
    secrets: tuple[str, ...]
    mode: str
    started_at: str = field(default_factory=lambda: utc_now())
    steps: list[dict[str, Any]] = field(default_factory=list)
    result: str = "running"
    smoke_success: bool = False
    full_social_acceptance: bool = False
    failure_code: str | None = None

    def safe_text(self, value: str) -> str:
        result = value
        for secret in self.secrets:
            if secret:
                result = result.replace(secret, "<redacted>")
        return result[:1800]

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.safe_text(value)
        if isinstance(value, dict):
            return {str(k): self.sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.sanitize(v) for v in value]
        return value

    def add(self, name: str, status: str, **details: Any) -> None:
        self.steps.append(self.sanitize({"name": name, "status": status, **details}))
        self.write()

    def write(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.sanitize(
            {
                "schema_version": 3,
                "mode": self.mode,
                "started_at": self.started_at,
                "finished_at": utc_now(),
                "result": self.result,
                "smoke_success": self.smoke_success,
                "full_social_acceptance": self.full_social_acceptance,
                "failure_code": self.failure_code,
                "steps": self.steps,
            }
        )
        self.output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


class LiveClient:
    def __init__(self, base_url: str, token: str, diagnostics: Diagnostics):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise LiveE2EError(
                "live_base_url_invalid",
                "STREET_STORY_LIVE_BASE_URL must be an HTTPS origin",
            )
        self.diag = diagnostics
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "StreetStory-Live-E2E/3",
            },
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    @staticmethod
    def error_code(response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except Exception:
            return None
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                return str(error.get("code") or "") or None
            if isinstance(body.get("detail"), str):
                return str(body["detail"])[:120]
        return None

    def request(
        self,
        method: str,
        path: str,
        *,
        step: str,
        expected: tuple[int, ...] = (200,),
        log: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        response = self.client.request(method, path, **kwargs)
        if response.status_code not in expected:
            code = self.error_code(response)
            self.diag.add(step, "failed", http_status=response.status_code, error_code=code)
            raise LiveE2EError(
                "http_request_failed",
                f"{step} returned HTTP {response.status_code} ({code or 'no_error_code'})",
            )
        if log:
            self.diag.add(step, "ok", http_status=response.status_code)
        return response

    def story(self, story_id: str) -> dict[str, Any]:
        payload = self.request(
            "GET", f"/v1/stories/{story_id}", step="story_readback", log=False
        ).json()
        if not isinstance(payload, dict):
            raise LiveE2EError("story_payload_invalid", "Story readback is not an object")
        return payload


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fixture_meta() -> dict[str, Any]:
    try:
        value = json.loads(FIXTURE_META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LiveE2EError("golden_fixture_metadata_invalid", str(exc)) from exc
    required = (
        "fixture_id",
        "download_url",
        "commons_page",
        "latitude",
        "longitude",
        "expected_object",
        "author",
        "license",
    )
    if any(not value.get(name) for name in required):
        raise LiveE2EError("golden_fixture_metadata_incomplete", "Golden fixture metadata is incomplete")
    return value


def download_fixture_photo(meta: dict[str, Any]) -> bytes:
    response = httpx.get(
        str(meta["download_url"]),
        headers={"User-Agent": WIKIMEDIA_USER_AGENT},
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
    )
    response.raise_for_status()
    data = response.content
    if not data or len(data) > 16 * 1024 * 1024:
        raise LiveE2EError("golden_fixture_size_invalid", "Fixture exceeds source upload budget")
    if b"JFIF" not in data[:64] and b"Exif" not in data[:64]:
        raise LiveE2EError("golden_fixture_not_jpeg", "Golden fixture is not the expected JPEG")
    return data


def synthesize_voice_chunk(root: Path, prefix: str, text: str) -> list[dict[str, Any]]:
    espeak = shutil.which("espeak")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not espeak or not ffmpeg or not ffprobe:
        raise LiveE2EError(
            "media_tools_missing",
            "Live harness requires espeak, ffmpeg and ffprobe on the GitHub runner",
        )
    wav = root / f"{prefix}.wav"
    m4a = root / f"{prefix}.m4a"
    subprocess.run(
        [espeak, "-v", "ru", "-s", "170", "-w", str(wav), text],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-movflags",
            "+faststart",
            str(m4a),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    duration = subprocess.check_output(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(m4a),
        ],
        text=True,
    ).strip()
    duration_ms = max(1, round(float(duration) * 1000))
    data = m4a.read_bytes()
    if b"ftyp" not in data[:64]:
        raise LiveE2EError("fixture_m4a_invalid", f"Generated {prefix} is not M4A")
    return [
        {
            "index": 0,
            "data": data,
            "sha256": hashlib.sha256(data).hexdigest(),
            "start_ms": 0,
            "end_ms": duration_ms,
            "wall_start_ms": 0,
            "wall_end_ms": duration_ms + 100,
        }
    ]


def open_voice_body(session_id: str, kind: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "kind": kind,
        "started_at": utc_now(),
        "timezone": "Europe/Kaliningrad",
        "device_label": "github-actions-live-e2e",
        "capture_policy": "voice_activity_auto_pause_v1",
        "audio": {
            "container": "mp4",
            "codec": "aac_lc",
            "mime_type": "audio/mp4",
            "sample_rate_hz": 16000,
            "channels": 1,
            "target_bitrate_bps": 32000,
        },
        "vad": {
            "engine": "webrtc_vad",
            "engine_version": "2.0.10-cf.4",
            "config_version": "vad-auto-pause-efficient-v1",
            "frame_ms": 30,
            "mode": 1,
        },
    }


def complete_body(session_id: str, kind: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    duration_ms = chunks[-1]["end_ms"] if chunks else 0
    return {
        "session_id": session_id,
        "kind": kind,
        "ended_at": utc_now(),
        "duration_ms": duration_ms,
        "wall_elapsed_ms": duration_ms + 200,
        "manual_pause_ms": 0,
        "auto_silence_skipped_ms": 0,
        "chunk_count": len(chunks),
        "chunks": [
            {
                "index": chunk["index"],
                "sha256": chunk["sha256"],
                "start_ms": chunk["start_ms"],
                "end_ms": chunk["end_ms"],
                "wall_start_ms": chunk["wall_start_ms"],
                "wall_end_ms": chunk["wall_end_ms"],
                "mime_type": "audio/mp4",
            }
            for chunk in chunks
        ],
    }


def sync_voice(
    client: LiveClient,
    story_id: str,
    session_id: str,
    kind: str,
    chunks: list[dict[str, Any]],
    run_tag: str,
) -> None:
    stable = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    open_body = open_voice_body(session_id, kind)
    open_key = f"live-open-{run_tag}-{stable}"[:128]
    receipt = client.request(
        "POST",
        f"/v1/stories/{story_id}/voice-sessions",
        step=f"voice_open_{stable}",
        headers={"Idempotency-Key": open_key},
        json=open_body,
    ).json()
    if receipt.get("session_id") != session_id:
        raise LiveE2EError("voice_session_identity_mismatch", "Voice session identity changed")
    for chunk in chunks:
        headers = {
            "Idempotency-Key": f"live-chunk-{run_tag}-{stable}-{chunk['index']}"[:128],
            "Content-Type": "audio/mp4",
            "X-Content-SHA256": chunk["sha256"],
            "X-Audio-Start-Ms": str(chunk["start_ms"]),
            "X-Audio-End-Ms": str(chunk["end_ms"]),
            "X-Wall-Start-Ms": str(chunk["wall_start_ms"]),
            "X-Wall-End-Ms": str(chunk["wall_end_ms"]),
        }
        receipt = client.request(
            "PUT",
            f"/v1/stories/{story_id}/voice-sessions/{session_id}/chunks/{chunk['index']}",
            step=f"voice_chunk_{stable}_{chunk['index']}",
            headers=headers,
            content=chunk["data"],
        ).json()
        received = {
            int(item["index"]): str(item["sha256"]).lower()
            for item in receipt.get("received", [])
            if isinstance(item, dict)
        }
        if received.get(chunk["index"]) != chunk["sha256"]:
            raise LiveE2EError("chunk_reconciliation_failed", "Voice chunk SHA did not read back")
    replay = client.request(
        "POST",
        f"/v1/stories/{story_id}/voice-sessions",
        step=f"voice_open_replay_{stable}",
        headers={"Idempotency-Key": open_key},
        json=open_body,
    ).json()
    expected_manifest = {chunk["index"]: chunk["sha256"] for chunk in chunks}
    replay_manifest = {
        int(item["index"]): str(item["sha256"]).lower()
        for item in replay.get("received", [])
        if isinstance(item, dict)
    }
    if replay_manifest != expected_manifest:
        raise LiveE2EError("voice_manifest_reconciliation_failed", "Voice replay changed manifest")
    complete = complete_body(session_id, kind, chunks)
    complete_key = f"live-complete-{run_tag}-{stable}"[:128]
    receipt = client.request(
        "POST",
        f"/v1/stories/{story_id}/voice-sessions/{session_id}/complete",
        step=f"voice_complete_{stable}",
        headers={"Idempotency-Key": complete_key},
        json=complete,
    ).json()
    if receipt.get("recording_finished") is not True:
        raise LiveE2EError("voice_not_durable", "Voice session did not become complete")
    replay_complete = client.request(
        "POST",
        f"/v1/stories/{story_id}/voice-sessions/{session_id}/complete",
        step=f"voice_complete_replay_{stable}",
        headers={"Idempotency-Key": complete_key},
        json=complete,
    ).json()
    if replay_complete.get("recording_finished") is not True:
        raise LiveE2EError("voice_complete_replay_failed", "Voice replay lost completion")


def quota_retry_evidence(story: dict[str, Any]) -> dict[str, Any] | None:
    processing = story.get("processing")
    if not isinstance(processing, dict):
        return None
    error = str(processing.get("last_error") or "")
    if processing.get("job_state") == "retry" and any(
        marker in error.lower() for marker in ("quota", "rate", "429")
    ):
        return processing
    return None


def wait_for_story(
    client: LiveClient,
    story_id: str,
    *,
    step: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: float,
    allowed_review_codes: set[str] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_marker: tuple[str, str | None] | None = None
    while time.monotonic() < deadline:
        story = client.story(story_id)
        publication = story.get("publication") if isinstance(story.get("publication"), dict) else {}
        marker = (str(story.get("state") or ""), str(publication.get("state") or "") or None)
        if marker != last_marker:
            error = story.get("error") if isinstance(story.get("error"), dict) else {}
            client.diag.add(
                f"{step}_state",
                "observed",
                state=marker[0],
                publication_state=marker[1],
                error_code=error.get("code"),
            )
            last_marker = marker
        quota = quota_retry_evidence(story)
        if quota is not None:
            raise LiveE2EError(
                "grounded_gemini_quota_retry",
                "Grounded Gemini is in durable quota/rate retry",
            )
        if predicate(story):
            return story
        if story.get("state") == "needs_review":
            error = story.get("error") if isinstance(story.get("error"), dict) else {}
            code = str(error.get("code") or "")
            if code not in (allowed_review_codes or set()):
                raise LiveE2EError("story_needs_review", f"{step} needs_review ({code or 'unknown'})")
        if story.get("state") == "visual_blocked":
            error = story.get("error") if isinstance(story.get("error"), dict) else {}
            raise LiveE2EError(
                "visual_blocked_not_accepted",
                f"Visual blocked ({error.get('code') or 'unknown'})",
            )
        time.sleep(POLL_SECONDS)
    raise LiveE2EError("poll_deadline_exceeded", f"Timed out waiting for {step}")


def validate_voice_messages(story: dict[str, Any], expected_sessions: list[str]) -> None:
    rows = story.get("voice_messages")
    if not isinstance(rows, list):
        raise LiveE2EError("voice_messages_missing", "Story has no voice_messages projection")
    ordered = [str(row.get("session_id")) for row in rows if isinstance(row, dict)]
    projected = [value for value in ordered if value in set(expected_sessions)]
    if projected != expected_sessions:
        raise LiveE2EError(
            "voice_message_order_changed",
            f"Expected ordered sessions {expected_sessions}, got {projected}",
        )
    by_id = {str(row.get("session_id")): row for row in rows if isinstance(row, dict)}
    for session_id in expected_sessions:
        raw = str(by_id[session_id].get("raw_transcript") or "").strip()
        display = str(by_id[session_id].get("display_text") or "").strip()
        if not raw or not display:
            raise LiveE2EError("voice_transcript_missing", f"{session_id} lacks raw/display text")
        if any(marker in display.lower() for marker in FILLER_MARKERS):
            raise LiveE2EError("display_text_not_cleaned", f"{session_id} display text has filler")


def supported_facts(story: dict[str, Any]) -> list[dict[str, Any]]:
    sources = story.get("sources")
    if not isinstance(sources, list) or int(story.get("source_count") or 0) != len(sources):
        raise LiveE2EError("source_projection_invalid", "Unique source count/list disagree")
    if not sources:
        raise LiveE2EError("source_projection_empty", "No unique sources were projected")
    for source in sources:
        if not isinstance(source, dict) or not str(source.get("title") or "").strip():
            raise LiveE2EError("source_title_missing", "A source has no display title")
        if not str(source.get("url") or "").startswith("https://"):
            raise LiveE2EError("source_url_invalid", "A source URL is not HTTPS")
    facts = story.get("facts")
    if not isinstance(facts, list):
        raise LiveE2EError("research_facts_missing", "Facts projection is absent")
    supported: list[dict[str, Any]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if fact.get("selected") is True and fact.get("evidence_supported") is not True:
            raise LiveE2EError("unsupported_fact_selected", "Unsupported fact was selected")
        if fact.get("evidence_supported") is not True:
            continue
        fact_sources = fact.get("sources") if isinstance(fact.get("sources"), list) else []
        if not fact_sources:
            raise LiveE2EError("supported_fact_without_sources", "Supported fact has no sources")
        for source in fact_sources:
            supports = source.get("supports") if isinstance(source, dict) else None
            if not isinstance(supports, list) or not supports:
                raise LiveE2EError("claim_support_missing", "Supported fact has no claim support")
            if any(not str(item.get("text") or "").strip() for item in supports if isinstance(item, dict)):
                raise LiveE2EError("claim_support_text_missing", "Claim support has empty text")
        supported.append(fact)
    if not supported:
        raise LiveE2EError("supported_facts_missing", "No evidence-supported claims were returned")
    return supported


def candidate_for_expected_object(story: dict[str, Any], expected: str) -> str | None:
    identity = story.get("visual_identity") if isinstance(story.get("visual_identity"), dict) else {}
    needle = expected.lower()
    for candidate in identity.get("candidates", []) if isinstance(identity.get("candidates"), list) else []:
        if not isinstance(candidate, dict):
            continue
        name = str(candidate.get("name") or "")
        if "brandenburg" in name.lower() or "бранденбург" in name.lower() or needle in name.lower():
            return str(candidate.get("candidate_id") or "") or None
    return None


def start_research(
    live: LiveClient,
    story_id: str,
    run_tag: str,
    ordinal: str,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "research"}
    if candidate_id:
        payload["candidate_id"] = candidate_id
    key = f"live-research-{ordinal}-{run_tag}"[:128]
    live.request(
        "POST",
        f"/v1/stories/{story_id}/facts",
        step=f"research_start_{ordinal}",
        headers={"Idempotency-Key": key},
        json=payload,
    )
    return wait_for_story(
        live,
        story_id,
        step=f"research_{ordinal}",
        predicate=lambda row: row.get("state") in {"review", "needs_review"},
        timeout_seconds=RESEARCH_TIMEOUT_SECONDS,
        allowed_review_codes={"visual_identity_uncertain"},
    )


def validate_telegram_destinations(capabilities: dict[str, Any]) -> list[dict[str, Any]]:
    destinations = capabilities.get("destinations")
    if not isinstance(destinations, list) or not destinations:
        raise LiveE2EError("telegram_destination_missing", "No MVP Telegram destination projected")
    rows = [row for row in destinations if isinstance(row, dict)]
    if any(str(row.get("provider") or "").lower() != "telegram" for row in rows):
        raise LiveE2EError("non_telegram_destination_exposed", "VK/MAX leaked into MVP capabilities")
    if any(str(row.get("status") or "") != "supported" for row in rows):
        raise LiveE2EError("telegram_destination_not_supported", "Projected Telegram destination is not supported")
    return rows


def is_explicit_test_destination(row: dict[str, Any]) -> bool:
    text = f"{row.get('alias', '')} {row.get('label', '')}".lower()
    return any(marker in text for marker in ("test", "тест", "safe", "e2e"))


def validate_internal_safe_alias(
    safe_alias: str,
    public_destinations: list[dict[str, Any]],
) -> str:
    if not safe_alias:
        raise LiveE2EError(
            "safe_test_destination_missing",
            "full_social requires SAFE_TEST_DESTINATION_ALIAS",
        )
    if not is_explicit_test_destination({"alias": safe_alias, "label": ""}):
        raise LiveE2EError(
            "safe_test_destination_not_explicitly_test",
            "Safe alias has no test/safe/e2e marker",
        )
    if any(str(row.get("alias") or "") == safe_alias for row in public_destinations):
        raise LiveE2EError(
            "safe_test_destination_exposed",
            "Internal SAFE_TEST_DESTINATION_ALIAS leaked into product capabilities",
        )
    return safe_alias


def run() -> int:
    base_url = os.environ.get("STREET_STORY_LIVE_BASE_URL", "").strip()
    token = os.environ.get("STREET_STORY_LIVE_TOKEN", "").strip()
    safe_alias = os.environ.get("SAFE_TEST_DESTINATION_ALIAS", "").strip()
    mode = os.environ.get("LIVE_E2E_MODE", "smoke").strip().lower()
    diagnostic_path = Path(
        os.environ.get("STREET_STORY_LIVE_DIAGNOSTIC", "live-e2e-artifacts/diagnostic.json")
    )
    diag = Diagnostics(
        diagnostic_path,
        tuple(value for value in (base_url, token, safe_alias) if value),
        mode,
    )
    if mode not in {"smoke", "full_social"}:
        diag.failure_code, diag.result = "live_mode_invalid", "failed"
        diag.write()
        return 2
    if not base_url or not token:
        diag.failure_code, diag.result = "live_secrets_missing", "configuration_required"
        diag.add(
            "configuration",
            "failed",
            base_url_configured=bool(base_url),
            token_configured=bool(token),
            safe_alias_configured=bool(safe_alias),
        )
        return 2

    live: LiveClient | None = None
    story_id: str | None = None
    publish_started = False
    cancel_confirmed = False
    cancel_key: str | None = None
    try:
        meta = fixture_meta()
        photo = download_fixture_photo(meta)
        photo_upload_id = uuid.uuid4().hex + uuid.uuid4().hex
        diag.add(
            "golden_fixture",
            "ok",
            fixture_id=meta["fixture_id"],
            commons_page=meta["commons_page"],
            author=meta["author"],
            license=meta["license"],
            source_url=meta["download_url"],
            photo_upload_id=photo_upload_id,
            expected_object=meta["expected_object"],
            owner_photo=False,
        )

        # Only audio synthesis uses temporary files; image bytes stay in RAM.
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="street-story-audio-", dir=diagnostic_path.parent) as tmp:
            root = Path(tmp)
            initial_chunks = [
                synthesize_voice_chunk(root, f"initial-{index}", text)
                for index, text in enumerate(INITIAL_VOICE_TEXTS, 1)
            ]
            refinement_chunks = synthesize_voice_chunk(root, "refinement", REFINEMENT_VOICE_TEXT)
            live = LiveClient(base_url, token, diag)
            health = live.request("GET", "/healthz", step="healthz").json()
            if health.get("ok") is not True:
                raise LiveE2EError("healthz_failed", "Backend health is not ok")

            run_tag = (
                f"{os.environ.get('GITHUB_RUN_ID', 'manual')}-"
                f"{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            client_story_id = f"live-e2e-{run_tag}"[:120]
            create_key = f"ss-live-create-{run_tag}"[:128]
            create_kwargs = {
                "files": {"photo": ("brandenburg-gate.jpg", photo, "image/jpeg")},
                "data": {
                    "client_story_id": client_story_id,
                    "photo_sha256": photo_upload_id,
                    "voice_protocol": "voice-chunks-v2",
                    "lat": str(meta["latitude"]),
                    "lon": str(meta["longitude"]),
                },
                "headers": {"Idempotency-Key": create_key, "X-Photo-SHA256": photo_upload_id},
            }
            story = live.request("POST", "/v1/stories", step="create_story", **create_kwargs).json()
            replay = live.request("POST", "/v1/stories", step="create_story_replay", **create_kwargs).json()
            story_id = str(story.get("id") or "")
            if not story_id or replay.get("id") != story_id:
                raise LiveE2EError("story_identity_mismatch", "Story create replay changed identity")

            initial_sessions: list[str] = []
            for index, chunks in enumerate(initial_chunks, 1):
                session = f"voice-initial-{index}-{run_tag}"[:120]
                initial_sessions.append(session)
                sync_voice(live, story_id, session, "initial", chunks, run_tag)
            before_research = live.story(story_id)
            if before_research.get("state") != "voice_ready":
                raise LiveE2EError(
                    "research_started_implicitly",
                    f"Expected voice_ready after three messages, got {before_research.get('state')}",
                )
            diag.add(
                "multi_voice_before_research",
                "ok",
                ordered_session_ids=initial_sessions,
                photo_upload_id=photo_upload_id,
            )

            story = start_research(live, story_id, run_tag, "initial")
            identity = story.get("visual_identity") if isinstance(story.get("visual_identity"), dict) else {}
            status = str(identity.get("status") or "")
            if status not in {"match", "owner_confirmed"}:
                candidate_id = candidate_for_expected_object(story, str(meta["expected_object"]))
                if not candidate_id:
                    raise LiveE2EError(
                        "golden_object_candidate_missing",
                        "Visual identity was uncertain and the independently known Brandenburg Gate candidate was absent",
                    )
                diag.add(
                    "visual_identity_owner_confirmation",
                    "required",
                    prior_status=status,
                    candidate_id=candidate_id,
                    owner_confirmation_not_vision=True,
                )
                story = start_research(live, story_id, run_tag, "confirmed", candidate_id)
                identity = story.get("visual_identity") if isinstance(story.get("visual_identity"), dict) else {}
            if str(identity.get("status") or "") not in {"match", "owner_confirmed"}:
                raise LiveE2EError("visual_identity_not_confirmed", "Golden object identity is not confirmed")
            validate_voice_messages(story, initial_sessions)
            research_voice_ids = [str(value) for value in story.get("research_voice_ids", [])]
            if research_voice_ids != initial_sessions:
                raise LiveE2EError("research_input_revision_order_changed", "Research voice order differs from captured order")
            supported = supported_facts(story)
            diag.add(
                "research_acceptance",
                "ok",
                research_revision=story.get("research_revision"),
                visual_identity={
                    "status": identity.get("status"),
                    "candidate_id": identity.get("candidate_id"),
                    "candidate_name": identity.get("candidate_name"),
                    "observations": identity.get("observations", [])[:4],
                },
                source_count=story.get("source_count"),
                sources=story.get("sources", []),
                supported_fact_count=len(supported),
            )

            removed = supported[0]
            keep_ids = [
                str(fact.get("fact_id"))
                for fact in supported[1:3]
                if str(fact.get("fact_id") or "")
            ]
            fact_key = f"live-facts-{run_tag}"[:128]
            fact_body = {"selected_fact_ids": keep_ids}
            selected = live.request(
                "POST",
                f"/v1/stories/{story_id}/facts",
                step="fact_selection",
                headers={"Idempotency-Key": fact_key},
                json=fact_body,
            ).json()
            selected_replay = live.request(
                "POST",
                f"/v1/stories/{story_id}/facts",
                step="fact_selection_replay",
                headers={"Idempotency-Key": fact_key},
                json=fact_body,
            ).json()
            selected_ids = {
                str(fact.get("fact_id"))
                for fact in selected.get("facts", [])
                if isinstance(fact, dict) and fact.get("selected") is True
            }
            replay_ids = {
                str(fact.get("fact_id"))
                for fact in selected_replay.get("facts", [])
                if isinstance(fact, dict) and fact.get("selected") is True
            }
            if selected_ids != set(keep_ids) or replay_ids != set(keep_ids):
                raise LiveE2EError("fact_selection_not_idempotent", "Fact selection replay differs")
            removed_text = str(removed.get("text") or "").strip()
            if removed_text and removed_text in str(selected.get("draft_text") or ""):
                raise LiveE2EError("deselected_fact_remained_in_draft", "Deselected fact remained in post draft")
            if removed_text and removed_text in str(selected.get("image_notes") or ""):
                raise LiveE2EError("deselected_fact_remained_in_image_notes", "Deselected fact remained in image notes")

            refinement_session = f"voice-refinement-{run_tag}"[:120]
            sync_voice(live, story_id, refinement_session, "refinement", refinement_chunks, run_tag)
            after_refinement_voice = live.story(story_id)
            if after_refinement_voice.get("state") != "review":
                raise LiveE2EError(
                    "refinement_started_research_implicitly",
                    f"Expected review before explicit update, got {after_refinement_voice.get('state')}",
                )
            if refinement_session in set(after_refinement_voice.get("research_voice_ids", [])):
                raise LiveE2EError("refinement_entered_old_revision", "New voice leaked into old research revision")
            story = start_research(live, story_id, run_tag, "refinement")
            all_sessions = [*initial_sessions, refinement_session]
            validate_voice_messages(story, all_sessions)
            if [str(value) for value in story.get("research_voice_ids", [])] != all_sessions:
                raise LiveE2EError("refinement_revision_order_changed", "Refinement research input order changed")
            after_by_id = {
                str(fact.get("fact_id")): fact
                for fact in story.get("facts", [])
                if isinstance(fact, dict)
            }
            removed_id = str(removed.get("fact_id") or "")
            if removed_id in after_by_id and after_by_id[removed_id].get("selected") is True:
                raise LiveE2EError("rejected_claim_returned", "Rejected stable claim was automatically re-enabled")
            if removed_text and removed_text in str(story.get("draft_text") or ""):
                raise LiveE2EError("rejected_claim_returned_to_draft", "Rejected claim returned to draft")
            if removed_text and removed_text in str(story.get("image_notes") or ""):
                raise LiveE2EError("rejected_claim_returned_to_image_notes", "Rejected claim returned to image notes")
            final_selected = [
                str(fact.get("fact_id"))
                for fact in story.get("facts", [])
                if isinstance(fact, dict)
                and fact.get("selected") is True
                and fact.get("evidence_supported") is True
            ]
            diag.add(
                "refinement_acceptance",
                "ok",
                ordered_session_ids=all_sessions,
                research_revision=story.get("research_revision"),
                selected_fact_ids=final_selected,
            )

            visual_key = f"live-visual-{run_tag}"[:128]
            visual_body = {"selected_fact_ids": final_selected}
            live.request(
                "POST",
                f"/v1/stories/{story_id}/visual",
                step="visual_start",
                headers={"Idempotency-Key": visual_key},
                json=visual_body,
            )
            story = wait_for_story(
                live,
                story_id,
                step="visual_processing",
                predicate=lambda row: row.get("state") == "ready_to_publish",
                timeout_seconds=VISUAL_TIMEOUT_SECONDS,
            )
            visual = story.get("visual") if isinstance(story.get("visual"), dict) else {}
            if str(visual.get("prompt_sha256") or "").lower() != OWNER_PROMPT_SHA256:
                raise LiveE2EError("owner_prompt_hash_mismatch", "Visual did not use exact owner prompt")
            required_visual = (
                "source_asset_ref",
                "operation_id",
                "selected_asset_ref",
                "prompt_version",
                "prompt_sha256",
                "content_revision",
            )
            missing = [name for name in required_visual if not visual.get(name)]
            if missing:
                raise LiveE2EError("visual_receipt_incomplete", f"Visual readback misses {missing}")
            asset_path = str(story.get("processed_image_url") or "")
            if not asset_path.startswith(("https://", "/v1/")):
                raise LiveE2EError("processed_image_url_missing", "Visual URL missing")
            diag.add(
                "visual_acceptance",
                "ok",
                prompt_sha256=visual.get("prompt_sha256"),
                content_revision=visual.get("content_revision"),
                operation_id=visual.get("operation_id"),
                selected_asset_ref=visual.get("selected_asset_ref"),
                source_asset_ref=visual.get("source_asset_ref"),
                identity_generation=(story.get("visual_identity") or {}).get("identity_generation"),
                image_url=asset_path,
            )

            capabilities = live.request("GET", "/v1/capabilities", step="capabilities").json()
            telegram_rows = validate_telegram_destinations(capabilities)
            diag.add(
                "telegram_only_projection",
                "ok",
                aliases=[row.get("alias") for row in telegram_rows],
            )
            diag.smoke_success = True
            diag.result = "smoke_accepted"
            diag.write()
            if mode == "smoke":
                return 0

            safe_alias = validate_internal_safe_alias(safe_alias, telegram_rows)
            publish_key = f"live-publish-{run_tag}"[:128]
            cancel_key = f"live-cancel-{run_tag}"[:128]
            publish_body = {
                "destinations": [safe_alias],
                "delay_minutes": 1500,
                "text_override": str(story.get("draft_text") or "")[:1024],
            }
            live.request(
                "POST",
                f"/v1/stories/{story_id}/publish",
                step="safe_publish_schedule",
                headers={"Idempotency-Key": publish_key},
                json=publish_body,
            )
            publish_started = True
            scheduled = wait_for_story(
                live,
                story_id,
                step="safe_publish_readback",
                predicate=lambda row: (
                    isinstance(row.get("publication"), dict)
                    and row["publication"].get("state") in {"scheduled", "verified"}
                ),
                timeout_seconds=SOCIAL_TIMEOUT_SECONDS,
            )
            publication = scheduled.get("publication")
            rows = scheduled.get("destinations") if isinstance(scheduled.get("destinations"), list) else []
            safe_rows = [row for row in rows if isinstance(row, dict) and row.get("alias") == safe_alias]
            if not isinstance(publication, dict) or not publication.get("publication_id"):
                raise LiveE2EError("publication_receipt_missing", "Scheduled publication lacks publication_id")
            if len(safe_rows) != 1 or safe_rows[0].get("status") not in {"scheduled", "verified"}:
                raise LiveE2EError("provider_schedule_readback_missing", "Native schedule readback missing")
            diag.add(
                "safe_schedule_acceptance",
                "ok",
                publication_id=publication.get("publication_id"),
                operation_id=publication.get("operation_id"),
                revision=publication.get("revision"),
                destination_alias=safe_alias,
                scheduled_for=scheduled.get("scheduled_for"),
                provider_status=safe_rows[0].get("status"),
                delay_minutes=1500,
            )

            live.request(
                "POST",
                f"/v1/stories/{story_id}/cancel",
                step="safe_publish_cancel",
                headers={"Idempotency-Key": cancel_key},
                json={},
            )
            cancelled = wait_for_story(
                live,
                story_id,
                step="safe_cancel_readback",
                predicate=lambda row: (
                    isinstance(row.get("publication"), dict)
                    and row["publication"].get("state") == "cancelled"
                ),
                timeout_seconds=SOCIAL_TIMEOUT_SECONDS,
            )
            cancel_rows = cancelled.get("destinations") if isinstance(cancelled.get("destinations"), list) else []
            safe_cancel_rows = [
                row for row in cancel_rows if isinstance(row, dict) and row.get("alias") == safe_alias
            ]
            if len(safe_cancel_rows) != 1 or safe_cancel_rows[0].get("status") != "cancelled":
                raise LiveE2EError("provider_cancel_readback_missing", "Native cancel readback missing")
            cancel_confirmed = True
            diag.full_social_acceptance = True
            diag.result = "full_social_accepted"
            diag.add(
                "safe_cancel_acceptance",
                "ok",
                publication_id=cancelled["publication"].get("publication_id"),
                cancel_operation_id=cancelled["publication"].get("cancel_operation_id"),
                destination_alias=safe_alias,
                provider_status=safe_cancel_rows[0].get("status"),
            )
            return 0

    except LiveE2EError as exc:
        diag.failure_code = exc.code
        diag.result = "failed" if not diag.smoke_success else "smoke_only_full_social_failed_closed"
        diag.add("failure", "failed", code=exc.code, message=str(exc))
        return 1
    except Exception as exc:
        diag.failure_code = f"unexpected_{type(exc).__name__}"
        diag.result = "failed"
        diag.add(
            "failure",
            "failed",
            code=diag.failure_code,
            message=f"{type(exc).__name__}: {exc}",
        )
        return 1
    finally:
        if live is not None and story_id and publish_started and not cancel_confirmed and cancel_key:
            try:
                live.request(
                    "POST",
                    f"/v1/stories/{story_id}/cancel",
                    step="best_effort_cleanup_cancel",
                    headers={"Idempotency-Key": cancel_key},
                    json={},
                )
                cleaned = wait_for_story(
                    live,
                    story_id,
                    step="best_effort_cleanup_readback",
                    predicate=lambda row: (
                        isinstance(row.get("publication"), dict)
                        and row["publication"].get("state") == "cancelled"
                    ),
                    timeout_seconds=SOCIAL_TIMEOUT_SECONDS,
                )
                rows = cleaned.get("destinations") if isinstance(cleaned.get("destinations"), list) else []
                safe_rows = [
                    row for row in rows if isinstance(row, dict) and row.get("alias") == safe_alias
                ]
                cancel_confirmed = bool(safe_rows and safe_rows[0].get("status") == "cancelled")
                diag.add(
                    "best_effort_cleanup",
                    "confirmed" if cancel_confirmed else "failed",
                    provider_cancelled=cancel_confirmed,
                )
            except Exception as cleanup_exc:
                diag.add(
                    "best_effort_cleanup",
                    "failed",
                    message=f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                )
        if live is not None:
            live.close()
        diag.write()


async def run_retained_story(args):
    from devcoveer_story_diag import load_installer

    installer = load_installer()
    for config in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        installer.require_mode(config, 0o600)
        os.environ.update(installer.parse_dotenv(config))
    os.environ["DATA_DIR"] = str(args.run / "data")
    from street_story.app import app
    from street_story.config import reveal
    from street_story.identity_proof import verified_physical_identity
    from live_interaction.socket_transport import serve_socket

    service = app.state.service
    sid = args.story_id
    report = {
        "status": "RUNNING",
        "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "story_id": sid,
        "source_run": str(args.run),
        "scope": "actual retained story, ordinary API + real provider + shared WSS relay; prepared PCM, no Android/physical microphone claim",
        "publication_dispatches": 0,
        "steps": [],
        "tool_trace": [],
        "explicit_fact_reconsideration": args.review_fact_ids,
    }
    report["snapshot_research_workers_unstarted"] = args.editorial_only
    output_name = args.output_name or ("e2e-continuation.json" if args.resume else "e2e.json")
    if Path(output_name).name != output_name or not output_name.endswith(".json"):
        raise RuntimeError("Output must be a fresh local JSON filename")
    out = args.run / output_name
    if args.resume:
        predecessor = args.run / (args.resume_from or "e2e.json")
        if predecessor.parent != args.run:
            raise RuntimeError("Predecessor must belong to this actual run")
        original = json.loads(predecessor.read_text())
        blocked_names = {"search_web", "get_research_chunk"}

        def known_closed(t):
            if t.get("state") in {"completed", "failed", "blocked_before_adapter"}:
                return True
            if (
                t.get("state") == "started"
                and t.get("name") in {"read_topic", "get_facts", "get_evidence"}
                and original.get("stop_acknowledged") is True
            ):
                return True
            return (
                t.get("state") == "started"
                and t.get("name") in blocked_names
                and any(
                    e.get("type") == "tool_result"
                    and e.get("name") == t["name"]
                    and e.get("status") == "error"
                    and e.get("message") == "Unexpected non-editorial action"
                    for e in original.get("events", [])
                )
            )

        allowed_predecessor = original.get("status") == "FAIL" or (
            args.revise_atomic_selection and original.get("status") == "REVIEW_REQUIRED" and original.get("mechanical_pass") is True
        )
        if not allowed_predecessor or any(
            (t["name"] == "generate_visual" and not (args.observe_existing_visual or args.revise_atomic_selection)) or not known_closed(t)
            for t in original.get("tool_trace", [])
        ):
            raise RuntimeError("Resume requires a known pre-visual failure; unknown visual cannot be replayed")
        report["continuation_of"] = str(predecessor)
    if out.exists():
        raise RuntimeError("Existing E2E operation must be read back before any new command")

    def save():
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        out.chmod(0o600)

    class VisualPreviewOnly:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            if name not in {"bootstrap", "ingress_asset", "read_asset", "visual", "status"}:
                raise RuntimeError("External publication is disabled for this task")
            return getattr(self.delegate, name)

    service.providers.vibepublish = VisualPreviewOnly(service.providers.vibepublish)
    # Match the existing shared-host PCM acceptance; the native relay/ACK codec
    # is the installed framework, not a copied consumer transport.
    host = app.state.live_host
    incoming = asyncio.Queue()
    events = []
    pending = {}
    latencies = []
    ack = 0
    seq = 0
    last_event_at = 0.0
    playback_until = 0.0
    session = None
    socket_task = None
    ping_task = None
    original_execute = host.adapter.execute_tool

    async def execute(session, call):
        name = call.get("name")
        entry = {
            "name": name,
            "id": call.get("id"),
            "args_keys": sorted((call.get("args") or {}).keys()),
            "at": time.time(),
            "state": "started",
        }
        report["tool_trace"].append(entry)
        save()
        if name not in {
            "read_topic",
            "get_facts",
            "get_evidence",
            "continue_story",
            "select_facts",
            "set_concept",
            "edit_text",
            "generate_visual",
            "get_review_packet",
            "get_review_context",
            "assess_review_packet",
            "finalize_fact_review",
            "repair_research_fact",
        }:
            entry.update(state="blocked_before_adapter", code="HARNESS_ACTION_NOT_ALLOWED")
            save()
            raise RuntimeError("Unexpected non-editorial action")
        try:
            result = await original_execute(session, call)
        except Exception as exc:
            entry.update(state="failed", code=str(getattr(exc, "code", type(exc).__name__)))
            save()
            raise
        entry.update(state="completed")
        save()
        return result

    host.adapter.execute_tool = execute
    original_initialize = host.adapter.initialize

    def initialize(**kwargs):
        initialized = original_initialize(**kwargs)
        cfg = initialized["configuration"]
        instruction = cfg.get("system_instruction", "")
        report.setdefault("actual_adapter_configurations", []).append(
            {
                "capability": initialized.get("capability"),
                "function_names": [f["name"] for f in cfg.get("functions", [])],
                "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                "context_field_bytes": {
                    k: len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode())
                    for k, v in initialized.get("context", {}).items()
                },
                "correction_route_present": "use continue_story(stage=review)" in instruction,
            }
        )
        save()
        return initialized

    host.adapter.initialize = initialize

    async def receive():
        return await incoming.get()

    async def send(payload):
        nonlocal ack, last_event_at, playback_until
        last_event_at = time.monotonic()
        if isinstance(payload, bytes):
            rate = struct.unpack("!III", payload[:12])[2]
            n = len(payload) - 12
            report["output_pcm_bytes"] = report.get("output_pcm_bytes", 0) + n
            playback_until = max(playback_until, time.monotonic()) + n / (rate * 2)
            return
        message = json.loads(payload)
        if message.get("type") == "audio_ack":
            ack = message["seq"]
            for number in list(pending):
                if number <= ack:
                    latencies.append(time.monotonic() - pending.pop(number))
        elif message.get("type") == "event":
            events.append(message["event"])
            report["events"] = events
            save()
        elif message.get("type") == "hello_ack":
            report["hello_ack"] = True

    async def close(code, reason):
        report["socket_close"] = {"code": code, "reason": reason}

    save()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + reveal(service.settings.device_token)},
    ) as client:

        async def read():
            response = await client.get("/v1/stories/" + sid)
            response.raise_for_status()
            return response.json()

        def selected(story):
            return {f["fact_id"] for f in story.get("facts", []) if f.get("selected")}

        def ready_selection(story):
            chosen = [f for f in story.get("facts", []) if f.get("selected")]
            return len(chosen) >= 3 and all(f.get("eligibility") == "eligible" for f in chosen)

        async def wait_step(name, tool, condition, checkpoint, tool_checkpoint, timeout=120):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = await read()
                if any(e.get("type") in {"error", "closed"} for e in events[checkpoint:]):
                    raise RuntimeError(name + "_provider_closed")
                if (
                    condition(current)
                    and any(t["name"] == tool and t["state"] == "completed" for t in report["tool_trace"][tool_checkpoint:])
                    and any(e.get("type") == "turn_complete" and e.get("seq", 0) > max(
                        (written.get("seq", 0) for written in events[checkpoint:]
                         if written.get("type") == "timing" and written.get("stage") == "tool_response_written"), default=0)
                        for e in events[checkpoint:])
                    and time.monotonic() > playback_until + 0.3
                    and time.monotonic() > last_event_at + 2
                ):
                    report["steps"].append({"name": name, "completed_at": time.time()})
                    save()
                    return current
                await asyncio.sleep(0.2)
            raise RuntimeError(name + "_readback_timeout")

        async def text_step(name, text, tool, condition):
            checkpoint = len(events)
            tool_checkpoint = len(report["tool_trace"])
            await incoming.put(json.dumps({"type": "input", "message": {"text": text}}, ensure_ascii=False))
            return await wait_step(name, tool, condition, checkpoint, tool_checkpoint)

        try:

            @asynccontextmanager
            async def editorial_workers():
                # Existing queue/worker, scoped to this story's visual operation. Do not
                # dispatch copied possibly-sent research operations from another process.
                async def visual_worker():
                    while True:
                        if not await service.run_once(claim_kind="visual", claim_story_id=sid):
                            await asyncio.sleep(0.2)

                worker = asyncio.create_task(visual_worker())
                try:
                    yield
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    await service.close()

            async with editorial_workers() if args.editorial_only else app.router.lifespan_context(app):
                first = await read()
                identity = first.get("visual_identity") or {}
                if not verified_physical_identity(identity):
                    raise RuntimeError("Actual automatic identity proof required")
                if not any(f.get("eligibility") == "eligible" for f in first.get("facts", [])):
                    raise RuntimeError("Actual verified facts required")
                if args.revise_atomic_selection:
                    prior = original.get("visual") or {}
                    visual = first.get("visual") or {}
                    if (
                        not args.resume
                        or args.observe_existing_visual
                        or args.review_fact_ids
                        or first["photo_sha256"] != original.get("photo_sha256")
                        or first.get("draft_text") != original.get("final_draft")
                        or selected(first) != set(original.get("selected_fact_ids") or [])
                        or not prior.get("selected_asset_ref")
                        or visual.get("selected_asset_ref") != prior["selected_asset_ref"]
                        or visual.get("operation_id") != prior.get("operation_id")
                    ):
                        raise RuntimeError("Editorial revision requires the exact already imported original visual and saved story context")
                    report["editorial_revision_of_closed_visual"] = prior["operation_id"]
                    save()
                report.update(
                    photo_sha256=identity.get("photo_sha256"),
                    identity_id=identity.get("candidate_id"),
                    proof_kind=identity.get("proof_kind"),
                    source_available=bool(service._source_photo_bytes(sid)),
                )
                save()
                session = await host.start(resource_id=sid, actor=None, model="gemini-3.8-live")
                report["session_id"] = session["session_id"]
                save()
                binding = host.open_socket(session_id=session["session_id"], resource_id=sid, ticket=session["socket_ticket"])
                socket_task = asyncio.create_task(serve_socket(binding, receive=receive, send=send, close=close))

                async def ping():
                    while True:
                        await asyncio.sleep(10)
                        await incoming.put('{"type":"ping"}')

                ping_task = asyncio.create_task(ping())
                await incoming.put(
                    json.dumps(
                        {
                            "type": "hello",
                            "protocol": "wl-live-v1",
                            "attempt_id": session["attempt_id"],
                            "cursor": 0,
                            "connection_generation": 1,
                        }
                    )
                )
                for _ in range(100):
                    if report.get("hello_ack"):
                        break
                    await asyncio.sleep(0.02)
                if not report.get("hello_ack"):
                    raise RuntimeError("Shared native hello_ack missing")
                if args.review_fact_ids:
                    review_ids = args.review_fact_ids.split(",")
                    report["review_before"] = [f for f in first["facts"] if f["fact_id"] in review_ids]
                    save()
                    if args.reuse_closed_review:
                        if not args.resume or set((original.get("explicit_fact_reconsideration") or "").split(",")) != set(review_ids):
                            raise RuntimeError("Closed review recovery must match its original requested scope")
                        from street_story import review_packets

                        with service.store.connection() as db:
                            current_bundle = review_packets.bundle(db, sid)
                            receipt = None
                            for row in db.execute(
                                "SELECT * FROM live_review_packets WHERE story_id=? AND result_json IS NOT NULL AND request_json IS NOT NULL ORDER BY rowid DESC",
                                (sid,),
                            ):
                                payload = json.loads(row["payload_json"])
                                requested = {item["id"] for item in payload["items"]}
                                if requested != set(review_ids):
                                    continue
                                if row["identity_generation"] != int(first.get("identity_generation") or 0):
                                    continue
                                if any(payload["bundle"].get(fid) != current_bundle.get(fid) for fid in review_ids):
                                    continue
                                if (payload.get("confirmed_identity") or {}).get("candidate_id") != identity.get("candidate_id"):
                                    continue
                                receipt = {
                                    "packet_ref": row["packet_ref"],
                                    "run_id": row["run_id"],
                                    "request_sha256": hashlib.sha256(row["request_json"].encode()).hexdigest(),
                                    "result_sha256": hashlib.sha256(row["result_json"].encode()).hexdigest(),
                                }
                                break
                        if receipt is None:
                            raise RuntimeError("No matching unchanged closed semantic receipt; no review write repeated")
                        if any(f.get("eligibility") not in {"eligible", "withheld"} for f in first["facts"] if f["fact_id"] in review_ids):
                            raise RuntimeError("Current review outcome remains unresolved")
                        report["review_reused_closed_receipt"] = receipt
                        report["steps"].append({"name": "explicit_fact_reconsideration_readback_only", "completed_at": time.time()})
                        revised = first
                    else:
                        revised = await text_step(
                            "explicit_fact_reconsideration",
                            "Перед выбором для публикации независимо перепроверь только эти сохранённые утверждения: "
                            + ", ".join(review_ids)
                            + ". Прочитай их собственные полные сохранённые доказательства через get_review_packet с fact_ids. "
                            "Проверь точный физический субъект этой истории и атомарность; не доверяй старому eligible. "
                            "Сохрани новое решение через finalize_fact_review, а если нужна смысловая правка, сохрани её и перепроверь новую версию. "
                            "Другие факты не меняй, новый поиск и распознавание не начинай. Текст и визуал пока не создавай.",
                            "finalize_fact_review",
                            lambda story: True,
                        )
                    report["review_after"] = [f for f in revised["facts"] if f["fact_id"] in review_ids]
                    with service.store.connection() as db:
                        placeholders = ",".join("?" for _ in review_ids)
                        report["canonical_review_after"] = [
                            dict(row)
                            for row in db.execute(
                                "SELECT poi_key,assertion_id,review_status,eligibility,reviewed_at FROM poi_research_assertions WHERE assertion_id IN ("
                                + placeholders
                                + ")",
                                review_ids,
                            )
                        ]
                    save()
                if args.observe_existing_visual:
                    if (
                        not args.resume
                        or args.review_fact_ids
                        or not first.get("draft_text")
                        or selected(first) != set(original.get("selected_fact_ids") or [])
                        or first["photo_sha256"] != original.get("photo_sha256")
                    ):
                        raise RuntimeError("Observation must preserve exact original SOURCE, selection and saved draft")
                    write_ids = {t["id"] for t in original["tool_trace"] if t["name"] == "edit_text" and t["state"] == "completed"}
                    with service.store.connection() as db:
                        receipt = db.execute(
                            "SELECT command_id,result_json FROM live_commands WHERE story_id=? AND tool_name='edit_text' ORDER BY rowid DESC LIMIT 1",
                            (sid,),
                        ).fetchone()
                    if (
                        not receipt
                        or receipt["command_id"] not in write_ids
                        or json.loads(receipt["result_json"]).get("draft_text") != first["draft_text"]
                    ):
                        raise RuntimeError("Saved draft differs from the exact original closed write")
                    choice = selected(first)
                    draft = first["draft_text"]
                    old_visual = (first.get("visual") or {}).get("operation_id")
                    report["selected_fact_ids"] = sorted(choice)
                    report["voice_reused_closed_segment"] = original.get("voice")
                    report["prior_completed_steps"] = original["steps"]
                    save()
                    await text_step(
                        "visual_observation",
                        "Получи уже созданное изображение этой истории через generate_visual с observe_existing_visual=true. Не запускай генерацию заново; исходник, выбор, замысел и текст сохрани. Ничего не публикуй.",
                        "generate_visual",
                        lambda story: bool(story.get("visual")),
                    )
                else:
                    selection_request = "Выбери для будущей публикации не менее трёх самостоятельных содержательных атомарных проверенных фактов из этой истории. Сначала прочитай полный get_facts и собственные доказательства выбранных утверждений через get_evidence. Не выбирай составное утверждение с несколькими независимо выбираемыми сведениями, даже если у него старый eligible. Сохрани выбор из готовых качественных фактов, не жди проверки остальных кандидатов. Пока не пиши текст и не создавай изображение."
                    if args.revise_atomic_selection:
                        selection_request = "Перейди к выбору уже готовых фактов для публикации. Прочитай существующие допущенные факты с их собственными источниками и сохрани другой набор не менее трёх самостоятельных содержательных атомарных фактов. Дата постройки, этажность, отдельный арендатор и отдельная деталь фасада — независимо выбираемые сведения, даже если источник объединяет их предложением. Сейчас нужен обычный выбор среди готовых фактов: не начинай новое исследование и не жди проверки остальных кандидатов. Объект, текст и изображение на этом шаге сохрани."
                    chosen = (
                        first
                        if args.resume and not args.revise_atomic_selection and not args.review_fact_ids and ready_selection(first)
                        else await text_step(
                            "fact_selection",
                            selection_request,
                            "select_facts",
                            lambda s: ready_selection(s) and (not args.revise_atomic_selection or selected(s) != selected(first)),
                        )
                    )
                    choice = selected(chosen)
                    report["selected_fact_ids"] = sorted(choice)
                    save()
                    if not args.resume or args.revise_atomic_selection or not chosen.get("publication_concept"):
                        chosen = await text_step(
                            "concept_dialog",
                            "Хочу спокойный городской рассказ о внимательном взгляде пешехода на знакомое здание. Обсуди и сохрани этот замысел; выбранные факты не меняй. Текст и изображение пока не создавай. Без новых исторических утверждений.",
                            "set_concept",
                            lambda s: bool(s.get("publication_concept")),
                        )
                    chosen = await read()
                    prior_draft_allowed = (
                        args.resume and not args.revise_atomic_selection and choice == set(original.get("selected_fact_ids") or [])
                    )
                    current_draft_commands = {
                        t["id"] for t in report["tool_trace"] if t["name"] == "edit_text" and t["state"] == "completed"
                    }
                    if chosen.get("draft_text") and selected(chosen) == choice and (prior_draft_allowed or current_draft_commands):
                        # A closed prior write may precede the failed step's observation boundary.
                        # Read its exact current receipt; never demand a redundant mutation.
                        with service.store.connection() as db:
                            receipt = db.execute(
                                "SELECT command_id,result_json FROM live_commands WHERE story_id=? AND tool_name='edit_text' ORDER BY rowid DESC LIMIT 1",
                                (sid,),
                            ).fetchone()
                            value = json.loads(receipt["result_json"]) if receipt else {}
                        if (
                            not receipt
                            or value.get("draft_text") != chosen["draft_text"]
                            or (not prior_draft_allowed and receipt["command_id"] not in current_draft_commands)
                        ):
                            raise RuntimeError("Saved draft has no matching closed write receipt")
                        report["draft_reused_closed_receipt"] = {
                            "command_id": receipt["command_id"],
                            "result_sha256": hashlib.sha256(receipt["result_json"].encode()).hexdigest(),
                        }
                        report["steps"].append({"name": "editable_draft_readback_only", "completed_at": time.time()})
                        save()
                        drafted = chosen
                    else:
                        drafted = await text_step(
                            "editable_draft",
                            "Напиши короткий ясный текст публикации на русском языке, до 700 знаков, только по выбранным проверенным фактам. Сохрани текст в этой истории, чтобы я мог его редактировать. Изображение пока не меняй.",
                            "edit_text",
                            lambda s: bool(s.get("draft_text")) and selected(s) == choice,
                        )
                    before_concept = drafted.get("publication_concept")
                    draft = drafted["draft_text"]
                    pcm = subprocess.check_output(
                        [
                            "ffmpeg",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-i",
                            str(args.voice),
                            "-ar",
                            "16000",
                            "-ac",
                            "1",
                            "-f",
                            "s16le",
                            "-",
                        ]
                    )
                    report["voice_source_sha256"] = hashlib.sha256(args.voice.read_bytes()).hexdigest()
                    checkpoint = len(events)
                    tool_checkpoint = len(report["tool_trace"])
                    await incoming.put(json.dumps({"type": "input", "message": {"activity_start": True}}))
                    start = time.monotonic()
                    for offset in range(0, len(pcm), 3200):
                        target = start + offset / 32000
                        await asyncio.sleep(max(0, target - time.monotonic()))
                        while len(pending) >= 4:
                            if time.monotonic() - min(pending.values()) > 2.5:
                                raise RuntimeError("Audio ACK stalled")
                            await asyncio.sleep(0.01)
                        seq += 1
                        pending[seq] = time.monotonic()
                        await incoming.put(
                            struct.pack("!III", 0x574C4131, seq, round(max(0, time.monotonic() - target) * 1000))
                            + pcm[offset : offset + 3200]
                        )
                    await incoming.put(json.dumps({"type": "input", "message": {"activity_end": True}}))
                    voiced = await wait_step(
                        "voice_concept",
                        "set_concept",
                        lambda s: s.get("publication_concept") != before_concept and selected(s) == choice,
                        checkpoint,
                        tool_checkpoint,
                    )
                    report["voice"] = {
                        "pcm_seconds": len(pcm) / 32000,
                        "frames_sent": seq,
                        "frames_ack": ack,
                        "max_ack_seconds": max(latencies, default=0),
                        "input_transcripts": [e.get("text") for e in events[checkpoint:] if e.get("type") == "input_transcript"],
                        "saved_concept": voiced.get("publication_concept"),
                        "selection_preserved": selected(voiced) == choice,
                        "draft_preserved": voiced["draft_text"] == draft,
                    }
                    if ack != seq:
                        raise RuntimeError("Incomplete audio ACK")
                    await text_step(
                        "draft_refinement",
                        "Учти последний сохранённый замысел и уточни текст. Сохрани выбранные факты полностью; не добавляй неподтверждённые сведения.",
                        "edit_text",
                        lambda s: bool(s.get("draft_text")) and not s.get("draft_needs_refresh") and selected(s) == choice,
                    )
                    current = await read()
                    draft = current["draft_text"]
                    old_visual = (current.get("visual") or {}).get("operation_id")
                    await text_step(
                        "visual_request",
                        "Теперь создай итоговый визуал для просмотра по исходному фото и сохранённому замыслу, с небольшим читаемым набором выбранных фактов. Текст и выбор фактов не меняй. Ничего не публикуй.",
                        "generate_visual",
                        lambda s: bool(s.get("visual")) or s.get("state") == "visual_processing",
                    )
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    current = await read()
                    visual = current.get("visual") or {}
                    if visual.get("selected_asset_ref") and (args.observe_existing_visual or visual.get("operation_id") != old_visual):
                        break
                    if current.get("error"):
                        raise RuntimeError("Visual failed: " + str(current["error"].get("code")))
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("Visual generation outcome pending; observe original operation")
                response = await client.get("/v1/assets/" + sid + "/processed")
                response.raise_for_status()
                if not response.content:
                    raise RuntimeError("Generated visual has no reviewable bytes")
                image = args.run / (Path(output_name).stem + "-visual.bin")
                if image.exists():
                    raise RuntimeError("Original visual evidence must not be overwritten")
                image.write_bytes(response.content)
                image.chmod(0o600)
                report["visual"] = {
                    k: visual.get(k) for k in ("operation_id", "selected_asset_ref", "source_asset_ref", "content_revision")
                }
                report["visual"].update(
                    bytes=len(response.content),
                    sha256=hashlib.sha256(response.content).hexdigest(),
                    content_type=response.headers.get("content-type"),
                )
                if current["draft_text"] != draft or selected(current) != choice:
                    raise RuntimeError("Visual changed owner editorial state")
                await host.stop(session_id=session["session_id"], resource_id=sid)
                report["stop_acknowledged"] = True
                session = None
                reopened = type(service)(service.settings)
                after = reopened.story(sid)
                if after["draft_text"] != draft or selected(after) != choice:
                    raise RuntimeError("Reopen lost selection or text")
                report.update(
                    status="REVIEW_REQUIRED",
                    mechanical_pass=True,
                    reopen_preserved=True,
                    semantic_review="Inspect actual voice transcript, concept, selected facts, draft and generated visual before PASS",
                    final_draft=draft,
                    saved_concept=after.get("publication_concept"),
                )
        except Exception as exc:
            report.update(status="FAIL", error={"type": type(exc).__name__, "code": str(getattr(exc, "code", str(exc)))[:200]})
        finally:
            if ping_task:
                ping_task.cancel()
                await asyncio.gather(ping_task, return_exceptions=True)
            if session:
                try:
                    await host.stop(session_id=session["session_id"], resource_id=sid)
                    report["stop_acknowledged"] = True
                except Exception as exc:
                    report["stop_error"] = str(getattr(exc, "code", type(exc).__name__))
            await incoming.put(None)
            if socket_task:
                try:
                    await asyncio.wait_for(socket_task, 5)
                except Exception:
                    socket_task.cancel()
                    await asyncio.gather(socket_task, return_exceptions=True)
            save()
    print(
        json.dumps({k: report.get(k) for k in ("status", "story_id", "session_id", "mechanical_pass", "error")}, ensure_ascii=False),
        flush=True,
    )
    return 0 if report.get("mechanical_pass") else 1


def retained_story_main():
    parser = argparse.ArgumentParser(
        description="Continue a retained actual story through the ordinary shared Live editor, voice and visual APIs."
    )
    parser.add_argument("--retained-story", action="store_true", required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--voice", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--editorial-only", action="store_true")
    parser.add_argument("--review-fact-ids", default="")
    parser.add_argument("--resume-from", default="")
    parser.add_argument("--output-name", default="")
    parser.add_argument("--reuse-closed-review", action="store_true")
    parser.add_argument("--observe-existing-visual", action="store_true")
    parser.add_argument("--revise-atomic-selection", action="store_true")
    return asyncio.run(run_retained_story(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(retained_story_main() if "--retained-story" in sys.argv else run())
