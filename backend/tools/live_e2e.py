from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import zlib
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
FIXTURE_LAT = 54.7064
FIXTURE_LON = 20.5117
INITIAL_VOICE_TEXTS = (
    "Э э, я я хочу рассказать про объект рядом с этими координатами в Калининграде.",
    "Проверь его историю, найди короткие факты с внешними ссылками и не выбирай неподтвержденное.",
)
REFINEMENT_VOICE_TEXTS = (
    "А а, еще еще уточни, пожалуйста, почему этот объект важен для города, сохрани уже выбранные подтвержденные факты.",
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
        return result[:1400]

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.safe_text(value)
        if isinstance(value, dict):
            return {str(k): self.sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(v) for v in value]
        return value

    def add(self, name: str, status: str, **details: Any) -> None:
        self.steps.append(self.sanitize({"name": name, "status": status, **details}))
        self.write()

    def write(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.sanitize(
            {
                "schema_version": 2,
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
        self.output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class LiveClient:
    def __init__(self, base_url: str, token: str, diagnostics: Diagnostics):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise LiveE2EError("live_base_url_invalid", "STREET_STORY_LIVE_BASE_URL must be an HTTPS origin")
        self.diag = diagnostics
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "StreetStory-Live-E2E/2",
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
        body = self.request(
            "GET",
            f"/v1/stories/{story_id}",
            step="story_readback",
            log=False,
        ).json()
        if not isinstance(body, dict):
            raise LiveE2EError("story_payload_invalid", "Story readback is not an object")
        return body


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_fixture_photo() -> bytes:
    width, height = 160, 120
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            if 24 < x < 136 and 30 < y < 104:
                rgb = (166, 105, 72)
            elif y > 92:
                rgb = (53, 57, 54)
            else:
                rgb = (219, 226, 222)
            row.extend(rgb)
        rows.append(b"\x00" + bytes(row))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + png_chunk(b"IEND", b"")
    )


def synthesize_voice_chunks(root: Path, prefix: str, texts: tuple[str, ...]) -> list[dict[str, Any]]:
    espeak = shutil.which("espeak")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not espeak or not ffmpeg or not ffprobe:
        raise LiveE2EError(
            "media_tools_missing",
            "Live harness requires espeak, ffmpeg and ffprobe on the GitHub runner",
        )
    chunks: list[dict[str, Any]] = []
    cursor = 0
    for index, text in enumerate(texts):
        wav = root / f"{prefix}-{index}.wav"
        m4a = root / f"{prefix}-{index}.m4a"
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
            raise LiveE2EError("fixture_m4a_invalid", f"Generated chunk {index} is not an MP4/M4A container")
        start_ms, end_ms = cursor, cursor + duration_ms
        cursor = end_ms
        chunks.append(
            {
                "index": index,
                "data": data,
                "sha256": hashlib.sha256(data).hexdigest(),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "wall_start_ms": start_ms,
                "wall_end_ms": end_ms + 100,
            }
        )
    return chunks


def open_voice_body(session_id: str, kind: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "kind": kind,
        "started_at": utc_now(),
        "timezone": "Etc/UTC",
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
                "index": c["index"],
                "sha256": c["sha256"],
                "start_ms": c["start_ms"],
                "end_ms": c["end_ms"],
                "wall_start_ms": c["wall_start_ms"],
                "wall_end_ms": c["wall_end_ms"],
                "mime_type": "audio/mp4",
            }
            for c in chunks
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
    open_body = open_voice_body(session_id, kind)
    open_key = f"live-open-{kind}-{run_tag}"[:128]
    receipt = client.request(
        "POST",
        f"/v1/stories/{story_id}/voice-sessions",
        step=f"{kind}_voice_open",
        headers={"Idempotency-Key": open_key},
        json=open_body,
    ).json()
    if receipt.get("session_id") != session_id:
        raise LiveE2EError("voice_session_identity_mismatch", f"{kind} voice session identity changed")

    for chunk in chunks:
        headers = {
            "Idempotency-Key": f"live-chunk-{kind}-{run_tag}-{chunk['index']}"[:128],
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
            step=f"{kind}_chunk_{chunk['index']}",
            headers=headers,
            content=chunk["data"],
        ).json()
        received = {
            int(item["index"]): str(item["sha256"]).lower()
            for item in receipt.get("received", [])
            if isinstance(item, dict)
        }
        if received.get(chunk["index"]) != chunk["sha256"]:
            raise LiveE2EError(
                "chunk_reconciliation_failed",
                f"{kind} chunk {chunk['index']} was not read back with its exact SHA",
            )

    replay = client.request(
        "POST",
        f"/v1/stories/{story_id}/voice-sessions",
        step=f"{kind}_voice_open_replay",
        headers={"Idempotency-Key": open_key},
        json=open_body,
    ).json()
    expected_manifest = {c["index"]: c["sha256"] for c in chunks}
    replay_manifest = {
        int(item["index"]): str(item["sha256"]).lower()
        for item in replay.get("received", [])
        if isinstance(item, dict)
    }
    if replay_manifest != expected_manifest:
        raise LiveE2EError("voice_manifest_reconciliation_failed", f"{kind} open replay changed the durable manifest")

    complete = complete_body(session_id, kind, chunks)
    complete_key = f"live-complete-{kind}-{run_tag}"[:128]
    receipt = client.request(
        "POST",
        f"/v1/stories/{story_id}/voice-sessions/{session_id}/complete",
        step=f"{kind}_voice_complete",
        headers={"Idempotency-Key": complete_key},
        json=complete,
    ).json()
    if receipt.get("recording_finished") is not True:
        raise LiveE2EError("voice_not_durable", f"{kind} voice did not become durably complete")
    final_manifest = {
        int(item["index"]): str(item["sha256"]).lower()
        for item in receipt.get("received", [])
        if isinstance(item, dict)
    }
    if final_manifest != expected_manifest:
        raise LiveE2EError("voice_complete_manifest_mismatch", f"{kind} completion changed the chunk manifest")
    replay_complete = client.request(
        "POST",
        f"/v1/stories/{story_id}/voice-sessions/{session_id}/complete",
        step=f"{kind}_voice_complete_replay",
        headers={"Idempotency-Key": complete_key},
        json=complete,
    ).json()
    if replay_complete.get("recording_finished") is not True:
        raise LiveE2EError("voice_complete_replay_failed", f"{kind} complete replay lost durable state")


def quota_retry_evidence(story: dict[str, Any]) -> dict[str, Any] | None:
    processing = story.get("processing")
    if not isinstance(processing, dict):
        return None
    error = str(processing.get("last_error") or "")
    lowered = error.lower()
    if processing.get("job_state") == "retry" and any(marker in lowered for marker in ("quota", "rate", "429")):
        return processing
    return None


def wait_for_story(
    client: LiveClient,
    story_id: str,
    *,
    step: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout_seconds: float,
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
            client.diag.add(f"{step}_gemini_retry", "failed", processing=quota)
            raise LiveE2EError(
                "grounded_gemini_quota_retry",
                "Grounded Gemini is in durable quota/rate retry; no fake facts were accepted",
            )
        if predicate(story):
            return story
        if story.get("state") == "needs_review":
            error = story.get("error") if isinstance(story.get("error"), dict) else {}
            raise LiveE2EError(
                "story_needs_review",
                f"{step} entered needs_review ({error.get('code') or 'no_error_code'})",
            )
        if story.get("state") == "visual_blocked":
            error = story.get("error") if isinstance(story.get("error"), dict) else {}
            raise LiveE2EError(
                "visual_blocked_not_accepted",
                f"Current product contract must not accept visual_blocked ({error.get('code') or 'no_error_code'})",
            )
        time.sleep(POLL_SECONDS)
    raise LiveE2EError("poll_deadline_exceeded", f"Timed out waiting for {step}")


def validate_voice_messages(story: dict[str, Any], expected_sessions: set[str]) -> dict[str, dict[str, Any]]:
    rows = story.get("voice_messages")
    if not isinstance(rows, list):
        raise LiveE2EError("voice_messages_missing", "Story has no voice_messages projection")
    by_id = {
        str(row.get("session_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("session_id")
    }
    missing = expected_sessions - set(by_id)
    if missing:
        raise LiveE2EError("voice_message_missing", f"Missing voice messages: {sorted(missing)}")
    for session_id in expected_sessions:
        raw = str(by_id[session_id].get("raw_transcript") or "").strip()
        display = str(by_id[session_id].get("display_text") or "").strip()
        if not raw:
            raise LiveE2EError("raw_transcript_missing", f"{session_id} has no durable raw_transcript")
        if not display:
            raise LiveE2EError("display_text_missing", f"{session_id} has no durable display_text")
        lowered = display.lower()
        if any(marker in lowered for marker in FILLER_MARKERS):
            raise LiveE2EError("display_text_not_cleaned", f"{session_id} display_text still contains a filler/repeat marker")
        if len(display) < 12:
            raise LiveE2EError("display_text_overcompressed", f"{session_id} display_text lost too much meaning")
    return by_id


def validate_research_story(story: dict[str, Any]) -> list[str]:
    provenance = story.get("research_provenance")
    if not isinstance(provenance, dict):
        raise LiveE2EError("research_provenance_missing", "Research provenance projection is missing")
    if provenance.get("osm_present") is not True:
        raise LiveE2EError("osm_evidence_missing", "OSM lookup was not durably present")
    if int(provenance.get("wikipedia_page_count") or 0) < 1:
        raise LiveE2EError("wikipedia_evidence_missing", "Wikipedia nearby lookup returned no durable page")
    if int(provenance.get("grounded_source_count") or 0) < 1:
        raise LiveE2EError("grounded_gemini_evidence_missing", "Grounded Gemini returned no grounding source")

    facts = story.get("facts")
    if not isinstance(facts, list) or not facts:
        raise LiveE2EError("research_facts_missing", "Research completed without candidate facts")
    supported_ids: list[str] = []
    for fact in facts:
        if not isinstance(fact, dict):
            raise LiveE2EError("research_fact_invalid", "Fact is not an object")
        supported = fact.get("evidence_supported") is True
        selected = fact.get("selected") is True
        sources = fact.get("sources") if isinstance(fact.get("sources"), list) else []
        https_urls = [
            source.get("url")
            for source in sources
            if isinstance(source, dict)
            and isinstance(source.get("url"), str)
            and source["url"].startswith("https://")
        ]
        if supported:
            if not https_urls:
                raise LiveE2EError(
                    "supported_fact_without_https_evidence",
                    "Evidence-supported fact has no HTTPS source URL",
                )
            fact_id = str(fact.get("fact_id") or "")
            if not fact_id:
                raise LiveE2EError("fact_id_missing", "Evidence-supported fact has no stable fact_id")
            supported_ids.append(fact_id)
        elif selected:
            raise LiveE2EError("unsupported_fact_selected", "Unsupported fact was selected")
    if not supported_ids:
        raise LiveE2EError(
            "https_evidence_missing",
            "No evidence-supported fact with HTTPS source URL was returned",
        )
    return supported_ids


def validate_primary_destinations(capabilities: dict[str, Any]) -> dict[str, dict[str, Any]]:
    destinations = capabilities.get("destinations")
    if not isinstance(destinations, list):
        raise LiveE2EError("destinations_missing", "VibePublish destination projection is missing")
    primary: dict[str, dict[str, Any]] = {}
    for row in destinations:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").lower().replace("ё", "е")
        provider = str(row.get("provider") or "").lower()
        if "полюбить калининград" in label and provider in {"telegram", "vk"}:
            primary[provider] = row
    if set(primary) != {"telegram", "vk"}:
        raise LiveE2EError(
            "primary_destination_projection_incomplete",
            f"Expected Telegram and VK 'Полюбить Калининград', found {sorted(primary)}",
        )
    for provider, row in primary.items():
        status = str(row.get("status") or "")
        if status not in {"supported", "needs_review"}:
            raise LiveE2EError(
                "primary_destination_ineligible",
                f"{provider} primary destination is {status or 'unknown'}",
            )
    return primary


def is_explicit_test_destination(row: dict[str, Any]) -> bool:
    text = f"{row.get('alias', '')} {row.get('label', '')}".lower()
    return any(marker in text for marker in ("test", "тест", "safe", "e2e"))


def run() -> int:
    base_url = os.environ.get("STREET_STORY_LIVE_BASE_URL", "").strip()
    token = os.environ.get("STREET_STORY_LIVE_TOKEN", "").strip()
    safe_alias = os.environ.get("SAFE_TEST_DESTINATION_ALIAS", "").strip()
    mode = os.environ.get("LIVE_E2E_MODE", "smoke").strip().lower()
    diagnostic_path = Path(
        os.environ.get("STREET_STORY_LIVE_DIAGNOSTIC", "live-e2e-artifacts/diagnostic.json")
    )
    diag = Diagnostics(diagnostic_path, tuple(value for value in (base_url, token, safe_alias) if value), mode)
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
        photo = make_fixture_photo()
        photo_sha = hashlib.sha256(photo).hexdigest()
        with tempfile.TemporaryDirectory(prefix="street-story-live-") as tmp:
            root = Path(tmp)
            initial_chunks = synthesize_voice_chunks(root, "initial", INITIAL_VOICE_TEXTS)
            refinement_chunks = synthesize_voice_chunks(root, "refinement", REFINEMENT_VOICE_TEXTS)
            diag.add(
                "fixtures",
                "ok",
                photo_sha256=photo_sha,
                initial_chunk_count=len(initial_chunks),
                refinement_chunk_count=len(refinement_chunks),
            )

            live = LiveClient(base_url, token, diag)
            health_client = httpx.Client(
                base_url=base_url.rstrip("/"),
                timeout=HTTP_TIMEOUT,
                follow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "StreetStory-Live-E2E/2"},
            )
            try:
                health = health_client.get("/healthz")
            finally:
                health_client.close()
            if health.status_code != 200 or health.json().get("ok") is not True:
                raise LiveE2EError("healthz_failed", f"/healthz returned HTTP {health.status_code}")
            diag.add("healthz", "ok", http_status=health.status_code, source_sha=health.json().get("source_sha"))

            run_tag = (
                f"{os.environ.get('GITHUB_RUN_ID', 'manual')}-"
                f"{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            client_story_id = f"live-e2e-{run_tag}"[:120]
            create_key = f"ss-live-create-{run_tag}"[:128]
            create_kwargs = {
                "files": {"photo": ("fixture.png", photo, "image/png")},
                "data": {
                    "client_story_id": client_story_id,
                    "photo_sha256": photo_sha,
                    "voice_protocol": "voice-chunks-v2",
                    "lat": str(FIXTURE_LAT),
                    "lon": str(FIXTURE_LON),
                },
                "headers": {"Idempotency-Key": create_key, "X-Photo-SHA256": photo_sha},
            }
            story = live.request("POST", "/v1/stories", step="create_story", **create_kwargs).json()
            replay_story = live.request(
                "POST",
                "/v1/stories",
                step="create_story_replay",
                **create_kwargs,
            ).json()
            story_id = str(story.get("id") or "")
            if (
                not story_id
                or story.get("client_story_id") != client_story_id
                or replay_story.get("id") != story_id
            ):
                raise LiveE2EError("story_identity_mismatch", "Create replay changed story identity")
            diag.add("story_identity", "ok", story_id=story_id, state=story.get("state"))

            initial_session = f"voice-initial-{run_tag}"[:120]
            sync_voice(live, story_id, initial_session, "initial", initial_chunks, run_tag)

            story = wait_for_story(
                live,
                story_id,
                step="initial_research",
                predicate=lambda row: row.get("state") == "review",
                timeout_seconds=RESEARCH_TIMEOUT_SECONDS,
            )
            supported = validate_research_story(story)
            validate_voice_messages(story, {initial_session})
            first_voice_snapshot = {
                row["session_id"]: (row.get("raw_transcript"), row.get("display_text"))
                for row in story.get("voice_messages", [])
                if isinstance(row, dict)
            }
            diag.add(
                "initial_research_acceptance",
                "ok",
                fact_count=len(story["facts"]),
                supported_fact_count=len(supported),
                provenance=story["research_provenance"],
            )

            chosen = supported[: min(2, len(supported))]
            facts_key = f"live-facts-{run_tag}"[:128]
            facts_body = {"selected_fact_ids": chosen}
            selected_story = live.request(
                "POST",
                f"/v1/stories/{story_id}/facts",
                step="manual_fact_selection",
                headers={"Idempotency-Key": facts_key},
                json=facts_body,
            ).json()
            selected_replay = live.request(
                "POST",
                f"/v1/stories/{story_id}/facts",
                step="manual_fact_selection_replay",
                headers={"Idempotency-Key": facts_key},
                json=facts_body,
            ).json()
            selected_now = {
                str(fact.get("fact_id"))
                for fact in selected_story.get("facts", [])
                if isinstance(fact, dict) and fact.get("selected") is True
            }
            selected_replayed = {
                str(fact.get("fact_id"))
                for fact in selected_replay.get("facts", [])
                if isinstance(fact, dict) and fact.get("selected") is True
            }
            if selected_now != set(chosen) or selected_replayed != set(chosen):
                raise LiveE2EError("fact_selection_not_idempotent", "Manual fact selection did not replay exactly")

            refinement_session = f"voice-refinement-{run_tag}"[:120]
            sync_voice(
                live,
                story_id,
                refinement_session,
                "refinement",
                refinement_chunks,
                run_tag,
            )
            refinement_key = f"live-refinement-{run_tag}"[:128]
            refinement_body = {
                "voice_session_id": refinement_session,
                "selected_fact_ids": chosen,
            }
            live.request(
                "POST",
                f"/v1/stories/{story_id}/refinements",
                step="queue_refinement",
                headers={"Idempotency-Key": refinement_key},
                json=refinement_body,
            )
            live.request(
                "POST",
                f"/v1/stories/{story_id}/refinements",
                step="queue_refinement_replay",
                headers={"Idempotency-Key": refinement_key},
                json=refinement_body,
            )
            story = wait_for_story(
                live,
                story_id,
                step="refinement_research",
                predicate=lambda row: row.get("state") == "review",
                timeout_seconds=RESEARCH_TIMEOUT_SECONDS,
            )
            validate_research_story(story)
            voices = validate_voice_messages(story, {initial_session, refinement_session})
            initial_after = (
                voices[initial_session].get("raw_transcript"),
                voices[initial_session].get("display_text"),
            )
            if initial_after != first_voice_snapshot[initial_session]:
                raise LiveE2EError(
                    "display_text_recomputed",
                    "Re-reading/refining the story changed the persisted initial raw/display text",
                )

            facts_after = {
                str(fact.get("fact_id")): fact
                for fact in story.get("facts", [])
                if isinstance(fact, dict)
            }
            missing_selected = [fact_id for fact_id in chosen if fact_id not in facts_after]
            lost_toggles = [
                fact_id
                for fact_id in chosen
                if fact_id in facts_after and facts_after[fact_id].get("selected") is not True
            ]
            if missing_selected or lost_toggles:
                raise LiveE2EError(
                    "refinement_fact_selection_not_preserved",
                    f"Refinement lost selected stable facts: missing={missing_selected}, toggled_off={lost_toggles}",
                )
            if any(
                fact.get("selected") is True and fact.get("evidence_supported") is not True
                for fact in facts_after.values()
            ):
                raise LiveE2EError(
                    "unsupported_fact_selected_after_refinement",
                    "Refinement selected an unsupported fact",
                )
            if not str(story.get("draft_text") or "").strip():
                raise LiveE2EError("publication_draft_missing", "Refinement completed without final publication draft")
            diag.add(
                "refinement_acceptance",
                "ok",
                chosen_fact_ids=chosen,
                voice_message_count=len(voices),
                draft_present=True,
            )

            visual_key = f"live-visual-{run_tag}"[:128]
            visual_body = {"selected_fact_ids": chosen}
            visual_started = live.request(
                "POST",
                f"/v1/stories/{story_id}/visual",
                step="visual_start",
                headers={"Idempotency-Key": visual_key},
                json=visual_body,
            ).json()
            visual_replay = live.request(
                "POST",
                f"/v1/stories/{story_id}/visual",
                step="visual_start_replay",
                headers={"Idempotency-Key": visual_key},
                json=visual_body,
            ).json()
            if visual_started.get("id") != visual_replay.get("id"):
                raise LiveE2EError("visual_mutation_replay_changed_story", "Visual replay changed the story identity")
            story = wait_for_story(
                live,
                story_id,
                step="visual_processing",
                predicate=lambda row: row.get("state") == "ready_to_publish",
                timeout_seconds=VISUAL_TIMEOUT_SECONDS,
            )
            visual = story.get("visual") if isinstance(story.get("visual"), dict) else {}
            required_visual = (
                "source_asset_ref",
                "operation_id",
                "visual_job_id",
                "candidate_id",
                "selected_asset_ref",
                "selected_sha256",
            )
            missing_visual = [name for name in required_visual if not visual.get(name)]
            if missing_visual:
                raise LiveE2EError(
                    "visual_receipt_incomplete",
                    f"Verified visual readback misses {missing_visual}",
                )
            selected_sha = str(visual["selected_sha256"]).lower()
            if len(selected_sha) != 64:
                raise LiveE2EError("visual_sha_invalid", "selected_sha256 is not a SHA-256 digest")
            asset_path = str(story.get("processed_image_url") or "")
            if not asset_path.startswith("/v1/assets/"):
                raise LiveE2EError("processed_asset_url_invalid", "Processed image is not exposed by authenticated Street Story readback")
            image_response = live.request(
                "GET",
                asset_path,
                step="processed_image_readback",
            )
            mime = image_response.headers.get("content-type", "").split(";", 1)[0]
            if mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise LiveE2EError("processed_asset_mime_invalid", f"Unexpected processed image MIME: {mime}")
            actual_sha = hashlib.sha256(image_response.content).hexdigest()
            header_sha = image_response.headers.get("x-content-sha256", "").lower()
            if actual_sha != selected_sha or header_sha != selected_sha:
                raise LiveE2EError(
                    "processed_asset_hash_mismatch",
                    "Processed image bytes/header do not match VibePublish selected_sha256",
                )
            final_visual_replay = live.request(
                "POST",
                f"/v1/stories/{story_id}/visual",
                step="visual_replay_after_verified",
                headers={"Idempotency-Key": visual_key},
                json=visual_body,
            ).json()
            replay_visual = (
                final_visual_replay.get("visual")
                if isinstance(final_visual_replay.get("visual"), dict)
                else {}
            )
            if (
                replay_visual.get("operation_id") != visual.get("operation_id")
                or replay_visual.get("selected_asset_ref") != visual.get("selected_asset_ref")
            ):
                raise LiveE2EError(
                    "visual_idempotency_changed_effect",
                    "Visual mutation replay changed operation or selected asset",
                )
            diag.add(
                "visual_acceptance",
                "ok",
                operation_id=visual["operation_id"],
                selected_asset_ref=visual["selected_asset_ref"],
                selected_sha256=selected_sha,
                bytes=len(image_response.content),
            )

            capabilities = live.request(
                "GET",
                "/v1/capabilities",
                step="capabilities",
            ).json()
            primary = validate_primary_destinations(capabilities)
            diag.add(
                "primary_destination_projection",
                "ok",
                telegram_status=primary["telegram"].get("status"),
                vk_status=primary["vk"].get("status"),
            )

            diag.smoke_success = True
            diag.result = "smoke_accepted"
            diag.write()
            if mode == "smoke":
                return 0

            if not safe_alias:
                raise LiveE2EError(
                    "safe_test_destination_missing",
                    "full_social requires SAFE_TEST_DESTINATION_ALIAS; smoke passed and no social side effect was attempted",
                )
            destinations = [
                row
                for row in capabilities.get("destinations", [])
                if isinstance(row, dict) and str(row.get("alias") or "") == safe_alias
            ]
            if len(destinations) != 1:
                raise LiveE2EError(
                    "safe_test_destination_not_projected",
                    "SAFE_TEST_DESTINATION_ALIAS was not uniquely returned by VibePublish",
                )
            safe_destination = destinations[0]
            if safe_destination.get("status") != "supported":
                raise LiveE2EError(
                    "safe_test_destination_not_supported",
                    f"Safe destination status is {safe_destination.get('status') or 'unknown'}",
                )
            if not is_explicit_test_destination(safe_destination):
                raise LiveE2EError(
                    "safe_test_destination_not_explicitly_test",
                    "Configured safe alias/label has no test/safe/e2e marker; refusing social side effect",
                )

            publish_key = f"live-publish-{run_tag}"[:128]
            cancel_key = f"live-cancel-{run_tag}"[:128]
            publish_body = {
                "destinations": [safe_alias],
                "delay_minutes": 60,
                "text_override": f"Street Story live E2E {run_tag}. Safe scheduled test; cancel immediately.",
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
            if not isinstance(publication, dict) or not publication.get("publication_id"):
                raise LiveE2EError("publication_receipt_missing", "Scheduled publication has no VibePublish publication_id")
            rows = scheduled.get("destinations") if isinstance(scheduled.get("destinations"), list) else []
            safe_rows = [row for row in rows if isinstance(row, dict) and row.get("alias") == safe_alias]
            if len(safe_rows) != 1 or safe_rows[0].get("status") not in {"scheduled", "verified"}:
                raise LiveE2EError(
                    "provider_schedule_readback_missing",
                    "Safe provider row did not confirm scheduled/verified state",
                )
            diag.add(
                "safe_schedule_acceptance",
                "ok",
                publication_id=publication.get("publication_id"),
                revision=publication.get("revision"),
                provider_status=safe_rows[0].get("status"),
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
            cancel_rows = (
                cancelled.get("destinations")
                if isinstance(cancelled.get("destinations"), list)
                else []
            )
            safe_cancel_rows = [
                row
                for row in cancel_rows
                if isinstance(row, dict) and row.get("alias") == safe_alias
            ]
            if len(safe_cancel_rows) != 1 or safe_cancel_rows[0].get("status") != "cancelled":
                raise LiveE2EError(
                    "provider_cancel_readback_missing",
                    "Safe provider row did not confirm cancelled state",
                )
            cancel_confirmed = True
            diag.full_social_acceptance = True
            diag.result = "full_social_accepted"
            diag.add(
                "safe_cancel_acceptance",
                "ok",
                publication_id=cancelled["publication"].get("publication_id"),
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
                    row
                    for row in rows
                    if isinstance(row, dict) and row.get("alias") == safe_alias
                ]
                cancel_confirmed = bool(
                    safe_rows and safe_rows[0].get("status") == "cancelled"
                )
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


if __name__ == "__main__":
    raise SystemExit(run())
