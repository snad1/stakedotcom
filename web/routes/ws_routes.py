"""WebSocket routes for real-time updates."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..websocket import manager, authenticate_ws
from ..services import system_metrics, stream_logs, SERVICES

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/metrics")
async def ws_metrics(ws: WebSocket):
    """Admin-only: system metrics every 3 seconds."""
    payload = await authenticate_ws(ws)
    if not payload or payload.get("role") != "admin":
        await ws.close(code=4001, reason="Unauthorized")
        return

    await manager.connect(ws, "admin:metrics")
    try:
        while True:
            metrics = system_metrics()
            await manager.send_to(ws, "metrics", metrics)
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws, "admin:metrics")


@router.websocket("/ws/logs/{service_label}")
async def ws_logs(ws: WebSocket, service_label: str):
    """Admin-only: live log streaming."""
    payload = await authenticate_ws(ws)
    if not payload or payload.get("role") != "admin":
        await ws.close(code=4001, reason="Unauthorized")
        return

    service_name = SERVICES.get(service_label)
    if not service_name:
        await ws.close(code=4002, reason="Unknown service")
        return

    await manager.connect(ws, f"admin:logs:{service_label}")
    try:
        async for line in stream_logs(service_name, lines=100):
            await manager.send_to(ws, "log", {"line": line})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws, f"admin:logs:{service_label}")


@router.websocket("/ws/session/{user_id}/{session_id}")
async def ws_session(ws: WebSocket, user_id: int, session_id: int):
    """Live session updates — available to admin or owning TG user."""
    payload = await authenticate_ws(ws)
    if not payload:
        await ws.close(code=4001, reason="Unauthorized")
        return

    # Check access: admin can view any, TG user can only view own
    if payload.get("role") == "tg_user":
        token_uid = int(payload["sub"].split(":")[1])
        if token_uid != user_id:
            await ws.close(code=4003, reason="Forbidden")
            return

    channel = f"session:{user_id}:{session_id}"
    await manager.connect(ws, channel)
    try:
        # Keep connection alive — actual updates pushed via broadcast from background task
        while True:
            # Periodically send session data from DB
            from ..bot_db import get_session
            session = get_session(user_id, session_id)
            if session:
                await manager.send_to(ws, "session_update", session)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws, channel)
