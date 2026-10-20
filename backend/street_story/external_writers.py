"""Caption writers outside Google: GLM through OpenCode, GPT Sol through Codex.

Owner decision 20.10.2026: the post text is written by GLM 5.3 (OpenCode); if it fails twice in a row,
by GPT-6.1 Sol (Codex); if Codex is unavailable or out of limit, by Gemini Flash-Lite. A comparison on
three real posts showed GLM and Sol writing the most connected, faithful stories.

The input is only what the author sees and controls in the app: the selected, verified facts, the
author's concept and request. Both clients run in an empty temporary work directory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

OPENCODE = os.getenv("STREET_STORY_OPENCODE_BIN", "/home/dev/.opencode/bin/opencode")
CODEX = os.getenv("STREET_STORY_CODEX_BIN", "/home/dev/.local/bin/codex")
GLM_MODEL = "opencode/glm-5.3-flash-free"
SOL_MODEL = "gpt-6.1-sol"
GLM_TIMEOUT = 180
SOL_TIMEOUT = 240
# After this many GLM failures in a row, Sol writes directly for a while (owner: "if GLM fails several times, Sol").
GLM_FAILURES_BEFORE_PAUSE = 2
GLM_PAUSE_SECONDS = 900
_glm_state = {"failures": 0, "paused_until": 0.0}
# The status line in the app names a model the author recognises.
DISPLAY = {GLM_MODEL: "GLM 5.3 Flash", SOL_MODEL: "GPT-6.1 Sol"}
NO_PHOTO = "\n\nФото дома в этом задании не передаётся: наблюдений по фото не делай, parts_on_photo оставь пустым."
ANSWER = ("\n\nОтветь одним JSON-объектом с полями throughline, parts_on_photo, outline, draft, check, text, "
          "image_notes, image_note_arrows — без пояснений вокруг.")


class ExternalWriterUnavailable(RuntimeError):
    pass


def parse_answer(raw: str) -> dict[str, Any]:
    """The writer's JSON object from a client's output (colour codes and banners around it are ignored)."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw or "")
    start, end = clean.find("{"), clean.rfind("}")
    while start != -1 and end > start:
        try:
            answer = json.loads(clean[start:end + 1])
        except ValueError:
            start = clean.find("{", start + 1)
            continue
        if isinstance(answer, dict) and str(answer.get("text") or "").strip():
            return answer
        break
    raise ExternalWriterUnavailable("writer_answer_unparsed")


def _failure(text: str) -> str:
    lowered = text.lower()
    if any(mark in lowered for mark in ("rate limit", "usage limit", "quota", "429")):
        return "writer_limit"
    return "writer_failed"


async def _run(argv: list[str], *, timeout: float, output_file: Path | None = None) -> str:
    with tempfile.TemporaryDirectory(prefix="street-story-writer-") as workdir:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError:
            raise ExternalWriterUnavailable("writer_client_missing") from None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise ExternalWriterUnavailable("writer_timeout") from None
        text = stdout.decode("utf-8", "replace")
        if output_file is not None and output_file.exists():
            text = output_file.read_text("utf-8", "replace")
        if process.returncode != 0:
            raise ExternalWriterUnavailable(_failure(text))
        return text


async def glm(prompt: str) -> dict[str, Any]:
    return parse_answer(await _run([OPENCODE, "run", "-m", GLM_MODEL, prompt + NO_PHOTO + ANSWER], timeout=GLM_TIMEOUT))


async def sol(prompt: str, photo_jpeg: bytes | None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="street-story-sol-") as scratch:
        argv = [CODEX, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "-m", SOL_MODEL]
        if photo_jpeg:
            photo = Path(scratch) / "photo.jpg"
            photo.write_bytes(photo_jpeg)
            argv += ["-i", str(photo)]
        last = Path(scratch) / "answer.txt"
        argv += ["-o", str(last), prompt + ("" if photo_jpeg else NO_PHOTO) + ANSWER]
        return parse_answer(await _run(argv, timeout=SOL_TIMEOUT, output_file=last))


async def write(prompt: str, photo_jpeg: bytes | None) -> tuple[dict[str, Any], str]:
    """GLM (one attempt; skipped for a while after repeated failures), then Sol. Raises when both fail."""
    if time.monotonic() >= _glm_state["paused_until"]:
        started = time.monotonic()
        try:
            answer = await glm(prompt)
            _glm_state["failures"] = 0
            logger.info("street_story_external_writer model=%s outcome=written seconds=%.1f",
                        GLM_MODEL, time.monotonic() - started)
            return answer, GLM_MODEL
        except ExternalWriterUnavailable as exc:
            _glm_state["failures"] += 1
            if _glm_state["failures"] >= GLM_FAILURES_BEFORE_PAUSE:
                _glm_state["paused_until"] = time.monotonic() + GLM_PAUSE_SECONDS
                _glm_state["failures"] = 0
            logger.warning("street_story_external_writer model=%s outcome=%s seconds=%.1f paused=%s",
                           GLM_MODEL, exc, time.monotonic() - started, _glm_state["paused_until"] > time.monotonic())
    started = time.monotonic()
    try:
        answer = await sol(prompt, photo_jpeg)
    except ExternalWriterUnavailable as exc:
        logger.warning("street_story_external_writer model=%s outcome=%s seconds=%.1f",
                       SOL_MODEL, exc, time.monotonic() - started)
        raise
    logger.info("street_story_external_writer model=%s outcome=written seconds=%.1f", SOL_MODEL, time.monotonic() - started)
    return answer, SOL_MODEL
