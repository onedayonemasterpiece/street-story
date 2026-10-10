from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .buildinfo import checkout_source_sha
from .config import Settings, reveal
from .live import create_live_host, live_history, record_live_diagnostic
from .live_socket import install_live_socket_routes, start_live_socket
from .runtime import RuntimeStreetStoryService
from live_interaction import LiveError
from .service import ConflictError, InvalidStateError, NotFoundError, StreetStoryService


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


LIVE_PROVIDER_AUDIO_CHARS = 16_000
LIVE_HTTP_AUDIO_CHARS = 48_000


class MemoryPhotoParser(MultiPartParser):
    # The stream limit is smaller than the spool threshold: no image can spill
    # onto disk, including a rejected oversized upload.
    spool_max_size = 32 * 1024 * 1024
    max_file_size = spool_max_size


async def photo_form(request: Request):
    async def bounded_stream():
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 17 * 1024 * 1024:
                raise MultiPartException('Selected photo exceeds the upload limit')
            yield chunk
    parser = MemoryPhotoParser(request.headers, bounded_stream(), max_files=1, max_fields=8)
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    try:
        photo = form.get('photo')
        if not isinstance(photo, UploadFile):
            raise HTTPException(status_code=422, detail='Photo is required')
        data = await photo.read()
        if len(data) > 16 * 1024 * 1024:
            raise HTTPException(status_code=413, detail='Selected photo exceeds the upload limit')
        return dict(form), data, photo.content_type or 'application/octet-stream'
    finally:
        await form.close()


def required_photo_field(form, field):
    value = form.get(field)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=422, detail=f'{field} is required')
    return value


def live_input_messages(message):
    """Split one mobile HTTP audio batch into ordered shared-host chunks."""
    if not isinstance(message, dict):
        return [message]
    audio = message.get("audio_base64")
    if not isinstance(audio, str) or len(audio) <= LIVE_PROVIDER_AUDIO_CHARS:
        return [message]
    if (
        set(message) != {"audio_base64"}
        or len(audio) > LIVE_HTTP_AUDIO_CHARS
        or len(audio) % 4
    ):
        raise LiveError("INVALID_ARGUMENT", "Audio batch is invalid")
    return [
        {"audio_base64": audio[offset : offset + LIVE_PROVIDER_AUDIO_CHARS]}
        for offset in range(0, len(audio), LIVE_PROVIDER_AUDIO_CHARS)
    ]


def create_app(settings: Settings | None = None, service: StreetStoryService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or RuntimeStreetStoryService(settings)
    service.recover_jobs()
    live_host = create_live_host(service, settings)
    source_sha = checkout_source_sha()

    async def worker_loop(*, visual_only=False, identity_only=False) -> None:
        lane = ({'claim_kind': 'identity'} if identity_only else
                {'claim_kind': 'identity_visual'} if visual_only else
                {'exclude_kind': ('identity', 'identity_visual')})
        name = asyncio.current_task().get_name()
        active = {}
        try:
            while True:
                for story_id, task in list(active.items()):
                    if task.done():
                        error = task.exception()
                        if error is not None:
                            logging.getLogger('uvicorn.error').error(
                                'street_story_worker_lane_failure lane=%s story_id=%s error_type=%s',
                                name, story_id, type(error).__name__)
                        del active[story_id]
                for story_id in service.worker_story_ids(**lane):
                    if story_id not in active:
                        active[story_id] = asyncio.create_task(
                            service.run_once(**lane, claim_story_id=story_id), name=name)
                await asyncio.sleep(settings.worker_poll_seconds)
        finally:
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(worker_loop(), name="street-story-worker")
        visual_task = asyncio.create_task(worker_loop(visual_only=True), name="street-story-identity-visual-worker")
        identity_task = asyncio.create_task(worker_loop(identity_only=True), name="street-story-identity-discovery-worker")
        try:
            yield
        finally:
            await live_host.stop_all()
            task.cancel()
            visual_task.cancel()
            identity_task.cancel()
            await asyncio.gather(task, visual_task, identity_task, return_exceptions=True)
            await service.close()

    app = FastAPI(title="Street Story", version="0.2.0", lifespan=lifespan)
    app.state.service = service
    app.state.live_host = live_host

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

    @app.exception_handler(LiveError)
    async def live_error_handler(_request: Request, exc: LiveError):
        status = 503 if exc.code in {"LIVE_UNAVAILABLE", "LIVE_PROVIDER_ERROR", "LIVE_PROVIDER_CLOSED", "LIVE_TIMEOUT"} else 409
        return error_response(status, exc.code.lower(), str(exc))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "worker_recovery": "enabled", "source_sha": source_sha}

    @app.post("/v1/stories", dependencies=[Depends(auth)])
    async def create_story(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        x_photo_sha256: str | None = Header(default=None, alias="X-Photo-SHA256"),
    ):
        form, data, mime = await photo_form(request)
        client_story_id = required_photo_field(form, 'client_story_id')
        photo_sha256 = required_photo_field(form, 'photo_sha256')
        voice_protocol = required_photo_field(form, 'voice_protocol')
        try:
            lat = float(form['lat']) if form.get('lat') else None
            lon = float(form['lon']) if form.get('lon') else None
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail='Invalid photo coordinates') from exc
        if x_photo_sha256 and x_photo_sha256.lower() != photo_sha256.lower():
            raise ConflictError("photo_upload_token_conflict", "Upload token header disagrees with multipart upload token")
        created = service.create_story(
            key=idem(idempotency_key), client_story_id=client_story_id, photo_sha256=photo_sha256,
            photo_mime_type=mime, photo_bytes=data,
            voice_protocol=voice_protocol, lat=lat, lon=lon,
            **({'location_provenance': {'kind': form['camera_coordinate_source']}}
                if form.get('camera_coordinate_source') else {}),
        )
        ensure_identity = getattr(service, "ensure_identity", None)
        return ensure_identity(created["id"]) if callable(ensure_identity) else created

    @app.get("/v1/stories", dependencies=[Depends(auth)])
    async def list_stories():
        return service.stories()

    @app.get("/v1/stories/{story_id}", dependencies=[Depends(auth)])
    async def get_story(story_id: str):
        return service.story(story_id)

    @app.delete("/v1/stories/{story_id}", dependencies=[Depends(auth)])
    async def delete_story(story_id: str):
        return service.delete_story(story_id)

    @app.post("/v1/stories/{story_id}/identity", dependencies=[Depends(auth)])
    async def ensure_story_identity(story_id: str):
        ensure_identity = getattr(service, "ensure_identity", None)
        if not callable(ensure_identity):
            raise ConflictError("identity_unavailable", "Automatic identity is unavailable")
        return ensure_identity(story_id)

    @app.post("/v1/stories/{story_id}/photo-location", dependencies=[Depends(auth)])
    async def recover_photo_location(story_id: str, request: Request):
        form, original, _ = await photo_form(request)
        token = required_photo_field(form, 'expected_photo_sha256')
        return service.recover_photo_location(story_id, token, original)

    @app.post("/v1/stories/{story_id}/diagnostics", dependencies=[Depends(auth)])
    async def photo_diagnostics(story_id: str, request: Request):
        from .identity_telemetry import client_fields, record_identity_event
        service.story(story_id)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(status_code=413, detail="Photo diagnostic payload exceeds limit")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(status_code=400, detail="Invalid photo diagnostic") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid photo diagnostic")
        event = body.get("event", "photo_import")
        if event not in {"photo_import", "live_start_requested", "stop_requested", "live_start_cancelled"}:
            raise HTTPException(status_code=400, detail="Unsupported client lifecycle diagnostic")
        record_identity_event(service, story_id, event, client_fields(body), source="android_import" if event == "photo_import" else "android_lifecycle")
        return {"ok": True, "story_id": story_id}

    @app.post("/v1/stories/{story_id}/live-sessions", dependencies=[Depends(auth)])
    async def start_live(story_id: str, request: Request):
        service.story(story_id)
        return await start_live_socket(
            live_host,
            story_id,
            request,
            history=live_history(service, story_id),
        )

    install_live_socket_routes(app, live_host, auth)

    @app.post("/v1/stories/{story_id}/live-sessions/{session_id}/diagnostics", dependencies=[Depends(auth)])
    async def live_diagnostics(story_id: str, session_id: str, request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 32_768:
                raise HTTPException(status_code=413, detail="Live diagnostic payload exceeds its bound")
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise HTTPException(status_code=400, detail="Invalid Live diagnostic payload") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Live diagnostic payload must be an object")
        event = str(body.get("event") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", event):
            raise HTTPException(status_code=400, detail="Invalid Live diagnostic event")
        fields = {key: value for key, value in body.items() if key != "event"}
        record_live_diagnostic(service, story_id, session_id, "android", event, fields)
        return {"ok": True, "session_id": session_id}

    @app.post("/v1/stories/{story_id}/live-sessions/{session_id}/input", dependencies=[Depends(auth)])
    async def live_input(story_id: str, session_id: str, request: Request):
        result = None
        for message in live_input_messages(await request.json()):
            result = await live_host.input(
                resource_id=story_id,
                session_id=session_id,
                actor={"subject": "street-story-device", "tenant_id": "street-story"},
                message=message,
            )
        return result

    @app.get("/v1/stories/{story_id}/live-sessions/{session_id}/events", dependencies=[Depends(auth)])
    async def live_events(story_id: str, session_id: str, after: int = 0):
        if after < 0:
            raise HTTPException(status_code=400, detail="after must be nonnegative")
        return live_host.events(
            resource_id=story_id,
            session_id=session_id,
            actor={"subject": "street-story-device", "tenant_id": "street-story"},
            after=after,
        )

    @app.post("/v1/stories/{story_id}/live-sessions/{session_id}/stop", dependencies=[Depends(auth)])
    async def stop_live(story_id: str, session_id: str):
        return await live_host.stop(
            resource_id=story_id,
            session_id=session_id,
            actor={"subject": "street-story-device", "tenant_id": "street-story"},
        )

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
            request_visual = getattr(service, 'request_visual', None)
            if callable(request_visual):
                return await request_visual(story_id, actual_key, body)
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

    @app.post('/v1/stories/{story_id}/research-control', dependencies=[Depends(auth)])
    async def research_control(story_id: str, request: Request,
                               idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
        return service.mutate_research_control(story_id, idem(idempotency_key), await request.json())

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
        data, mime = await service.asset(story_id)
        return Response(data, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/capabilities", dependencies=[Depends(auth)])
    async def capabilities():
        return await service.capabilities()

    return app


app = create_app()
