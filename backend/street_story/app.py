from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .buildinfo import checkout_source_sha
from .config import Settings, reveal
from .runtime import RuntimeStreetStoryService
from .service import ConflictError, InvalidStateError, NotFoundError, StreetStoryService


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(settings: Settings | None = None, service: StreetStoryService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or RuntimeStreetStoryService(settings)
    service.recover_jobs()
    source_sha = checkout_source_sha()

    async def worker_loop() -> None:
        while True:
            worked = await service.run_once()
            if not worked:
                await asyncio.sleep(settings.worker_poll_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(worker_loop(), name="street-story-worker")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Street Story", version="0.2.0", lifespan=lifespan)
    app.state.service = service

    async def auth(authorization: str | None = Header(default=None)) -> None:
        expected = reveal(settings.device_token)
        if not expected:
            raise HTTPException(status_code=503, detail="STREET_STORY_DEVICE_TOKEN is not configured")
        supplied = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Bearer device token required")

    def idem(key: str | None) -> str:
        if not key:
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        return key

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request: Request, exc: ConflictError):
        return error_response(409, exc.code, str(exc))

    @app.exception_handler(InvalidStateError)
    async def state_handler(_request: Request, exc: InvalidStateError):
        return error_response(409, exc.code, str(exc))

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, exc: NotFoundError):
        return error_response(404, "not_found", str(exc))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "worker_recovery": "enabled", "source_sha": source_sha}

    @app.post("/v1/stories", dependencies=[Depends(auth)])
    async def create_story(
        photo: UploadFile = File(...),
        client_story_id: str = Form(...),
        photo_sha256: str = Form(...),
        voice_protocol: str = Form(...),
        lat: float | None = Form(default=None),
        lon: float | None = Form(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_photo_sha256: str | None = Header(default=None, alias="X-Photo-SHA256"),
    ):
        if x_photo_sha256 and x_photo_sha256.lower() != photo_sha256.lower():
            raise ConflictError("photo_digest_header_conflict", "X-Photo-SHA256 disagrees with multipart photo_sha256")
        data = await photo.read()
        return service.create_story(
            key=idem(idempotency_key), client_story_id=client_story_id, photo_sha256=photo_sha256,
            photo_mime_type=photo.content_type or "application/octet-stream", photo_bytes=data,
            voice_protocol=voice_protocol, lat=lat, lon=lon,
        )

    @app.get("/v1/stories", dependencies=[Depends(auth)])
    async def list_stories():
        return service.stories()

    @app.get("/v1/stories/{story_id}", dependencies=[Depends(auth)])
    async def get_story(story_id: str):
        return service.story(story_id)

    @app.post("/v1/stories/{story_id}/voice-sessions", dependencies=[Depends(auth)])
    async def open_voice(story_id: str, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return service.open_voice(story_id, idem(idempotency_key), await request.json())

    @app.put("/v1/stories/{story_id}/voice-sessions/{session_id}/chunks/{index}", dependencies=[Depends(auth)])
    async def put_chunk(
        story_id: str, session_id: str, index: int, request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_content_sha256: str = Header(alias="X-Content-SHA256"),
        x_audio_start_ms: int = Header(alias="X-Audio-Start-Ms"), x_audio_end_ms: int = Header(alias="X-Audio-End-Ms"),
        x_wall_start_ms: int = Header(alias="X-Wall-Start-Ms"), x_wall_end_ms: int = Header(alias="X-Wall-End-Ms"),
    ):
        return service.put_chunk(
            story_id, session_id, index, idem(idempotency_key), x_content_sha256, await request.body(),
            {"start_ms": x_audio_start_ms, "end_ms": x_audio_end_ms, "wall_start_ms": x_wall_start_ms, "wall_end_ms": x_wall_end_ms},
            request.headers.get("content-type", "audio/mp4").split(";", 1)[0],
        )

    @app.post("/v1/stories/{story_id}/voice-sessions/{session_id}/complete", dependencies=[Depends(auth)])
    async def complete_voice(story_id: str, session_id: str, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return service.complete_voice(story_id, session_id, idem(idempotency_key), await request.json())

    async def mutation(story_id: str, request: Request, kind: str, key: str | None):
        body = await request.json()
        actual_key = idem(key)
        if kind == "facts":
            return service.mutate_facts(story_id, actual_key, body)
        if kind == "refinements":
            return service.mutate_refinement(story_id, actual_key, body)
        if kind == "visual":
            return service.mutate_visual(story_id, actual_key, body)
        if kind == "publish":
            return service.mutate_publish(story_id, actual_key, body)
        if kind == "cancel":
            cancel = getattr(service, "mutate_cancel", None)
            if cancel is None:
                raise InvalidStateError("cancel_not_supported", "This Street Story runtime does not expose publication cancellation")
            return cancel(story_id, actual_key, body)
        raise AssertionError(kind)

    @app.post("/v1/stories/{story_id}/facts", dependencies=[Depends(auth)])
    async def facts(story_id: str, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await mutation(story_id, request, "facts", idempotency_key)

    @app.post("/v1/stories/{story_id}/refinements", dependencies=[Depends(auth)])
    async def refinements(story_id: str, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await mutation(story_id, request, "refinements", idempotency_key)

    @app.post("/v1/stories/{story_id}/visual", dependencies=[Depends(auth)])
    async def visual(story_id: str, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await mutation(story_id, request, "visual", idempotency_key)

    @app.post("/v1/stories/{story_id}/publish", dependencies=[Depends(auth)])
    async def publish(story_id: str, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await mutation(story_id, request, "publish", idempotency_key)

    @app.post("/v1/stories/{story_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(story_id: str, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return await mutation(story_id, request, "cancel", idempotency_key)

    @app.get("/v1/assets/{story_id}/processed", dependencies=[Depends(auth)])
    async def asset(story_id: str):
        data, mime, sha = service.asset(story_id)
        return Response(data, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-SHA256": sha, "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/capabilities", dependencies=[Depends(auth)])
    async def capabilities():
        return await service.capabilities()

    return app


app = create_app()
