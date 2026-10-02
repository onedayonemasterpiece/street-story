"""Street Story authorization/routes for the shared WSS transport.

This adapter owns only the HTTP boundary. Framing, ticket validation, event push,
backpressure, provider transport and interrupted-turn admission live in the SDK.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from live_interaction import LiveError
from live_interaction.socket_transport import SOCKET_PROTOCOL, same_origin, serve_socket, socket_ticket

ACTOR = {"subject": "street-story-device", "tenant_id": "street-story"}


async def start_live_socket(host, story_id: str, request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            raise HTTPException(status_code=413, detail="Live bootstrap exceeds its bound")
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="Invalid Live bootstrap") from None
    if not isinstance(body, dict) or set(body) - {"attempt_id", "transport"}:
        raise HTTPException(status_code=400, detail="Unsupported Live bootstrap fields")
    if body.get("transport", "wss") != "wss":
        raise HTTPException(status_code=400, detail="Unsupported Live transport")
    started = await host.start(resource_id=story_id, actor=ACTOR, attempt_id=body.get("attempt_id"))
    return {
        **started,
        "socket_url": f"/v1/stories/{story_id}/live-sessions/{started['session_id']}/socket",
        "transport": "wss",
    }


def install_live_socket_routes(app, host, auth) -> None:
    @app.post("/v1/stories/{story_id}/live-sessions/{session_id}/socket-ticket", dependencies=[Depends(auth)])
    async def renew_ticket(story_id: str, session_id: str):
        result = host.issue_socket_ticket(session_id=session_id, resource_id=story_id, actor=ACTOR)
        return {**result, "socket_url": f"/v1/stories/{story_id}/live-sessions/{session_id}/socket"}

    @app.websocket("/v1/stories/{story_id}/live-sessions/{session_id}/socket")
    async def live_socket(websocket: WebSocket, story_id: str, session_id: str):
        # Ticket is provided ONLY in the subprotocol. Never accept query
        # credentials, bearer headers as a ticket substitute, or foreign origin.
        if websocket.scope.get("query_string") or not same_origin(
            websocket.headers.get("origin"), websocket.headers.get("host")
        ):
            await websocket.close(code=1008)
            return
        try:
            ticket = socket_ticket(websocket.scope.get("subprotocols", []))
            binding = host.open_socket(session_id=session_id, resource_id=story_id, ticket=ticket)
        except LiveError:
            await websocket.close(code=1008)
            return
        try:
            await websocket.accept(subprotocol=SOCKET_PROTOCOL)
        except Exception:
            await binding.close()
            raise

        async def receive():
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return None
            if message["type"] == "websocket.disconnect":
                return None
            return message.get("bytes") if message.get("bytes") is not None else message.get("text")

        async def send(payload):
            if isinstance(payload, bytes):
                await websocket.send_bytes(payload)
            else:
                await websocket.send_text(payload)

        async def close(code, reason):
            await websocket.close(code=code, reason=reason)

        await serve_socket(binding, receive=receive, send=send, close=close)
