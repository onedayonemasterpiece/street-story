from __future__ import annotations

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
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=45.0, pool=10.0)
POLL_SECONDS = 5.0
RESEARCH_TIMEOUT_SECONDS = 12 * 60
VISUAL_TIMEOUT_SECONDS = 3 * 60
EXPECTED_VISUAL_BLOCKER = "vibepublish_media_ingress_not_enabled"
FIXTURE_LAT = 54.7064
FIXTURE_LON = 20.5117
VOICE_TEXTS = (
    "Тест Street Story. Найди объект рядом с координатами в Калининграде и проверь его историю.",
    "Дай короткие факты с внешними ссылками. Неподтвержденные факты не выбирай.",
)


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
        return result[:1000]

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
                "schema_version": 1,
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
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "StreetStory-Live-E2E/1"},
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

    def request(self, method: str, path: str, *, step: str, expected: tuple[int, ...] = (200,), **kwargs: Any) -> httpx.Response:
        response = self.client.request(method, path, **kwargs)
        if response.status_code not in expected:
            code = self.error_code(response)
            self.diag.add(step, "failed", http_status=response.status_code, error_code=code)
            raise LiveE2EError("http_request_failed", f"{step} returned HTTP {response.status_code} ({code or 'no_error_code'})")
        self.diag.add(step, "ok", http_status=response.status_code)
        return response


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
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + png_chunk(b"IEND", b"")


def synthesize_voice_chunks(root: Path) -> list[dict[str, Any]]:
    espeak = shutil.which("espeak")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not espeak or not ffmpeg or not ffprobe:
        raise LiveE2EError("media_tools_missing", "Live harness requires espeak, ffmpeg and ffprobe on the GitHub runner")
    chunks: list[dict[str, Any]] = []
    cursor = 0
    for index, text in enumerate(VOICE_TEXTS):
        wav = root / f"voice-{index}.wav"
        m4a = root / f"voice-{index}.m4a"
        subprocess.run([espeak, "-v", "ru", "-s", "175", "-w", str(wav), text], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-ar", "16000", "-ac", "1", "-c:a", "aac", "-b:a", "32k", "-movflags", "+faststart", str(m4a)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        duration = subprocess.check_output([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(m4a)], text=True).strip()
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


def validate_research_story(story: dict[str, Any]) -> list[str]:
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
        https_urls = [s.get("url") for s in sources if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith("https://")]
        if supported:
            if not https_urls:
                raise LiveE2EError("supported_fact_without_https_evidence", "Evidence-supported fact has no HTTPS source URL")
            fact_id = str(fact.get("fact_id") or "")
            if not fact_id:
                raise LiveE2EError("fact_id_missing", "Evidence-supported fact has no stable fact_id")
            supported_ids.append(fact_id)
        elif selected:
            raise LiveE2EError("unsupported_fact_selected", "Unsupported fact was selected")
    if not supported_ids:
        raise LiveE2EError("https_evidence_missing", "No evidence-supported fact with HTTPS source URL was returned")
    return supported_ids


def validate_visual_block(story: dict[str, Any]) -> bool:
    if story.get("state") != "visual_blocked":
        return False
    error = story.get("error") if isinstance(story.get("error"), dict) else {}
    if error.get("code") != EXPECTED_VISUAL_BLOCKER:
        raise LiveE2EError("unexpected_visual_blocker", f"Expected {EXPECTED_VISUAL_BLOCKER}, got {error.get('code') or 'none'}")
    return True


def wait_for_story(client: LiveClient, story_id: str, *, step: str, terminal: set[str], timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_state: str | None = None
    while time.monotonic() < deadline:
        story = client.request("GET", f"/v1/stories/{story_id}", step=step).json()
        state = str(story.get("state") or "")
        if state != last_state:
            error = story.get("error") if isinstance(story.get("error"), dict) else {}
            client.diag.add(f"{step}_state", "observed", state=state, error_code=error.get("code"))
            last_state = state
        if state in terminal:
            return story
        time.sleep(POLL_SECONDS)
    raise LiveE2EError("poll_deadline_exceeded", f"Timed out waiting for {step}")


def complete_body(session_id: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    duration_ms = chunks[-1]["end_ms"] if chunks else 0
    return {
        "session_id": session_id,
        "kind": "initial",
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


def run() -> int:
    base_url = os.environ.get("STREET_STORY_LIVE_BASE_URL", "").strip()
    token = os.environ.get("STREET_STORY_LIVE_TOKEN", "").strip()
    safe_alias = os.environ.get("SAFE_TEST_DESTINATION_ALIAS", "").strip()
    mode = os.environ.get("LIVE_E2E_MODE", "smoke").strip().lower()
    diagnostic_path = Path(os.environ.get("STREET_STORY_LIVE_DIAGNOSTIC", "live-e2e-artifacts/diagnostic.json"))
    diag = Diagnostics(diagnostic_path, tuple(v for v in (base_url, token, safe_alias) if v), mode)
    if mode not in {"smoke", "full_social"}:
        diag.failure_code, diag.result = "live_mode_invalid", "failed"
        diag.write()
        return 2
    if not base_url or not token:
        diag.failure_code, diag.result = "live_secrets_missing", "configuration_required"
        diag.add("configuration", "failed", base_url_configured=bool(base_url), token_configured=bool(token), safe_alias_configured=bool(safe_alias))
        return 2

    live: LiveClient | None = None
    try:
        photo = make_fixture_photo()
        photo_sha = hashlib.sha256(photo).hexdigest()
        with tempfile.TemporaryDirectory(prefix="street-story-live-") as tmp:
            chunks = synthesize_voice_chunks(Path(tmp))
            diag.add("fixtures", "ok", photo_sha256=photo_sha, chunk_count=len(chunks))

            live = LiveClient(base_url, token, diag)
            health_client = httpx.Client(base_url=base_url.rstrip("/"), timeout=HTTP_TIMEOUT, follow_redirects=False, headers={"Accept": "application/json", "User-Agent": "StreetStory-Live-E2E/1"})
            try:
                health = health_client.get("/healthz")
            finally:
                health_client.close()
            if health.status_code != 200 or health.json().get("ok") is not True:
                raise LiveE2EError("healthz_failed", f"/healthz returned HTTP {health.status_code}")
            diag.add("healthz", "ok", http_status=health.status_code)

            run_tag = f"{os.environ.get('GITHUB_RUN_ID', 'manual')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}-{uuid.uuid4().hex[:8]}"
            client_story_id = f"live-e2e-{run_tag}"[:120]
            story = live.request(
                "POST",
                "/v1/stories",
                step="create_story",
                files={"photo": ("fixture.png", photo, "image/png")},
                data={"client_story_id": client_story_id, "photo_sha256": photo_sha, "voice_protocol": "voice-chunks-v2", "lat": str(FIXTURE_LAT), "lon": str(FIXTURE_LON)},
                headers={"Idempotency-Key": f"ss-live-create-{run_tag}"[:128], "X-Photo-SHA256": photo_sha},
            ).json()
            story_id = str(story.get("id") or "")
            if not story_id or story.get("client_story_id") != client_story_id:
                raise LiveE2EError("story_identity_mismatch", "Backend returned a different story identity")
            diag.add("story_identity", "ok", story_id=story_id, state=story.get("state"))

            session_id = f"voice-live-{run_tag}"[:120]
            voice_body = {
                "session_id": session_id,
                "kind": "initial",
                "started_at": utc_now(),
                "timezone": "Etc/UTC",
                "device_label": "github-actions-live-e2e",
                "capture_policy": "voice_activity_auto_pause_v1",
                "audio": {"container": "mp4", "codec": "aac_lc", "mime_type": "audio/mp4", "sample_rate_hz": 16000, "channels": 1, "target_bitrate_bps": 32000},
                "vad": {"engine": "webrtc_vad", "engine_version": "2.0.10-cf.4", "config_version": "vad-auto-pause-efficient-v1", "frame_ms": 30, "mode": 1},
            }
            voice_key = f"ss-live-voice-{run_tag}"[:128]
            opened = live.request("POST", f"/v1/stories/{story_id}/voice-sessions", step="open_voice", json=voice_body, headers={"Idempotency-Key": voice_key}).json()
            if opened.get("session_id") != session_id or opened.get("received") != []:
                raise LiveE2EError("voice_open_receipt_invalid", "Initial voice receipt is not empty or belongs to another session")

            for chunk in chunks:
                receipt = live.request(
                    "PUT",
                    f"/v1/stories/{story_id}/voice-sessions/{session_id}/chunks/{chunk['index']}",
                    step=f"put_chunk_{chunk['index']}",
                    content=chunk["data"],
                    headers={
                        "Idempotency-Key": f"ss-live-chunk-{run_tag}-{chunk['index']}"[:128],
                        "Content-Type": "audio/mp4",
                        "X-Content-SHA256": chunk["sha256"],
                        "X-Audio-Start-Ms": str(chunk["start_ms"]),
                        "X-Audio-End-Ms": str(chunk["end_ms"]),
                        "X-Wall-Start-Ms": str(chunk["wall_start_ms"]),
                        "X-Wall-End-Ms": str(chunk["wall_end_ms"]),
                    },
                ).json()
                received = {int(x["index"]): str(x["sha256"]).lower() for x in receipt.get("received", [])}
                if received.get(chunk["index"]) != chunk["sha256"]:
                    raise LiveE2EError("chunk_receipt_mismatch", f"Chunk {chunk['index']} was not durably acknowledged")

            expected_received = [{"index": c["index"], "sha256": c["sha256"]} for c in chunks]
            reconciled = live.request("POST", f"/v1/stories/{story_id}/voice-sessions", step="reopen_voice_reconciliation", json=voice_body, headers={"Idempotency-Key": voice_key}).json()
            if reconciled.get("received") != expected_received or reconciled.get("recording_finished") is not False:
                raise LiveE2EError("voice_reconciliation_mismatch", "Re-opened voice manifest differs from uploaded chunks")

            completed = live.request(
                "POST",
                f"/v1/stories/{story_id}/voice-sessions/{session_id}/complete",
                step="complete_voice",
                json=complete_body(session_id, chunks),
                headers={"Idempotency-Key": f"ss-live-complete-{run_tag}"[:128]},
            ).json()
            if completed.get("recording_finished") is not True or completed.get("received") != expected_received:
                raise LiveE2EError("voice_complete_receipt_invalid", "Exact manifest was not durably completed")

            story = wait_for_story(live, story_id, step="research", terminal={"review", "needs_review"}, timeout_seconds=RESEARCH_TIMEOUT_SECONDS)
            if story.get("state") != "review":
                error = story.get("error") if isinstance(story.get("error"), dict) else {}
                raise LiveE2EError("research_not_review", f"Research ended in {story.get('state')} ({error.get('code') or 'no_error_code'})")
            supported_ids = validate_research_story(story)
            diag.add("research_invariants", "ok", fact_count=len(story.get("facts", [])), supported_fact_count=len(supported_ids), place_present=bool(story.get("place_name")))

            chosen = supported_ids[: min(3, len(supported_ids))]
            selected_story = live.request(
                "POST",
                f"/v1/stories/{story_id}/facts",
                step="select_facts",
                json={"selected_fact_ids": chosen},
                headers={"Idempotency-Key": f"ss-live-facts-{run_tag}"[:128]},
            ).json()
            if {str(f["fact_id"]) for f in selected_story.get("facts", []) if f.get("selected") is True} != set(chosen):
                raise LiveE2EError("fact_selection_mismatch", "Backend did not preserve the requested supported fact selection")
            validate_research_story(selected_story)

            live.request(
                "POST",
                f"/v1/stories/{story_id}/visual",
                step="start_visual",
                json={"selected_fact_ids": chosen},
                headers={"Idempotency-Key": f"ss-live-visual-{run_tag}"[:128]},
            )
            visual_story = wait_for_story(live, story_id, step="visual", terminal={"visual_blocked", "ready_to_publish", "needs_review"}, timeout_seconds=VISUAL_TIMEOUT_SECONDS)

            capabilities = live.request("GET", "/v1/capabilities", step="capabilities").json()
            destinations = capabilities.get("destinations") if isinstance(capabilities, dict) else None
            if not isinstance(destinations, list) or not destinations:
                raise LiveE2EError("destinations_missing", "Street Story/VibePublish capabilities returned no real destinations")
            provider_statuses = sorted({f"{d.get('provider', 'unknown')}:{d.get('status', 'unknown')}" for d in destinations if isinstance(d, dict)})
            diag.add("destination_projection", "ok", destination_count=len(destinations), provider_statuses=provider_statuses, safe_alias_configured=bool(safe_alias))

            if validate_visual_block(visual_story):
                diag.smoke_success = True
                if mode == "full_social":
                    raise LiveE2EError("full_social_visual_blocked", f"Full social acceptance is unavailable while visual is blocked by {EXPECTED_VISUAL_BLOCKER}")
                diag.result = "smoke_success_expected_visual_blocked"
                diag.add("visual_boundary", "ok", state="visual_blocked", error_code=EXPECTED_VISUAL_BLOCKER, acceptance="smoke_only")
                print("Street Story live E2E: smoke success; expected visual ingress blocker observed; full social acceptance not claimed.")
                return 0

            if visual_story.get("state") != "ready_to_publish":
                error = visual_story.get("error") if isinstance(visual_story.get("error"), dict) else {}
                raise LiveE2EError("visual_not_ready", f"Visual ended in {visual_story.get('state')} ({error.get('code') or 'no_error_code'})")

            diag.smoke_success = True
            if not safe_alias:
                if mode == "full_social":
                    raise LiveE2EError("safe_test_destination_required", "Full social acceptance requires SAFE_TEST_DESTINATION_ALIAS")
                diag.result = "smoke_success_visual_ready_social_skipped"
                diag.add("social_acceptance", "skipped", reason="safe_test_destination_not_configured")
                print("Street Story live E2E: smoke success; visual ready; social acceptance skipped because no safe test alias is configured.")
                return 0

            matching = [d for d in destinations if isinstance(d, dict) and d.get("alias") == safe_alias]
            if len(matching) != 1:
                raise LiveE2EError("safe_test_destination_not_projected", "Configured safe test destination is not uniquely projected by live capabilities")

            # Current Street Story HTTPS API has no cancel/cleanup mutation. Fail closed before any scheduled provider effect.
            raise LiveE2EError(
                "full_social_cleanup_contract_missing",
                "Visual is ready and a safe alias is configured, but the current Street Story HTTPS API has no cleanup/cancel mutation; no publication was created",
            )
    except LiveE2EError as exc:
        diag.failure_code = exc.code
        diag.result = "failed" if not diag.smoke_success else "smoke_success_full_social_not_accepted"
        diag.add("final", "failed", failure_code=exc.code, message=str(exc))
        print(f"Street Story live E2E: {diag.result}; code={exc.code}. See diagnostic artifact.")
        return 1
    except Exception as exc:
        diag.failure_code, diag.result = "unexpected_exception", "failed"
        diag.add("final", "failed", failure_code="unexpected_exception", message=f"{type(exc).__name__}: {exc}")
        print("Street Story live E2E: failed with unexpected_exception. See diagnostic artifact.")
        return 1
    finally:
        if live is not None:
            live.close()
        diag.write()


if __name__ == "__main__":
    sys.exit(run())
