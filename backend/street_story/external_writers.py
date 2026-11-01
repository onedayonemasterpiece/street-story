"""Caption writers outside Google: GLM through OpenCode, GPT Sol through Codex.

Owner decision 01.11.2026: GPT-6.1 Sol (reasoning medium) writes the post text and card
lettering first; GLM 5.3 Flash is the fallback, followed by Gemini lite in the caller.
The 16-run medium/high comparison found equal quality and high was 1.6 times slower.

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
# Owner decision 01.11.2026: Sol medium first, GLM as fallback.
# GLM variant low prevents excessive thinking; generous timeouts only catch non-answers.
GLM_VARIANT = "low"
GLM_ATTEMPTS = 3
GLM_TIMEOUT = 240
SOL_TIMEOUT = 240
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


async def glm(prompt: str, *, answer_instruction: str = ANSWER) -> dict[str, Any]:
    # A fixed title: otherwise OpenCode asks a paid model to name the session, and that call fails.
    return parse_answer(await _run([OPENCODE, "run", "--title", "Street Story caption", "--variant", GLM_VARIANT,
                                    "-m", GLM_MODEL, prompt + NO_PHOTO + answer_instruction], timeout=GLM_TIMEOUT))


async def sol(prompt: str, photo_jpeg: bytes | None, *, answer_instruction: str = ANSWER) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="street-story-sol-") as scratch:
        argv = [CODEX, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "-m", SOL_MODEL,
                "-c", "model_reasoning_effort=medium"]
        if photo_jpeg:
            photo = Path(scratch) / "photo.jpg"
            photo.write_bytes(photo_jpeg)
            argv += ["-i", str(photo)]
        last = Path(scratch) / "answer.txt"
        argv += ["-o", str(last), prompt + ("" if photo_jpeg else NO_PHOTO) + answer_instruction]
        return parse_answer(await _run(argv, timeout=SOL_TIMEOUT, output_file=last))


async def write(prompt: str, photo_jpeg: bytes | None, *, answer_instruction: str = ANSWER,
                validate=None) -> tuple[dict[str, Any], str]:
    """Owner decision 01.11: Sol medium first, then GLM; Gemini remains the caller's fallback."""
    def accepted(answer):
        if validate is not None:
            try:
                validate(answer)
            except (ValueError, KeyError, TypeError) as exc:
                raise ExternalWriterUnavailable("writer_answer_invalid") from exc
        return answer

    started = time.monotonic()
    try:
        answer = accepted(await sol(prompt, photo_jpeg, answer_instruction=answer_instruction))
        logger.info("street_story_external_writer model=%s outcome=written seconds=%.1f",
                    SOL_MODEL, time.monotonic() - started)
        return answer, SOL_MODEL
    except ExternalWriterUnavailable as exc:
        logger.warning("street_story_external_writer model=%s outcome=%s seconds=%.1f",
                       SOL_MODEL, exc, time.monotonic() - started)
    for attempt in range(1, GLM_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            answer = accepted(await glm(prompt, answer_instruction=answer_instruction))
            logger.info("street_story_external_writer model=%s attempt=%s outcome=written seconds=%.1f",
                        GLM_MODEL, attempt, time.monotonic() - started)
            return answer, GLM_MODEL
        except ExternalWriterUnavailable as exc:
            logger.warning("street_story_external_writer model=%s attempt=%s outcome=%s seconds=%.1f",
                           GLM_MODEL, attempt, exc, time.monotonic() - started)
    raise ExternalWriterUnavailable("all_external_writers_unavailable")
