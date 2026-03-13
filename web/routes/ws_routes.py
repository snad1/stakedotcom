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

    # Track the last bet id we've already sent so we only push new bets
    last_bet_id: int = 0

    try:
        # Keep connection alive — send session stats + any new bets every 2 s
        while True:
            from ..bot_db import get_session, _connect_ro
            session = get_session(user_id, session_id)
            if session:
                await manager.send_to(ws, "session_update", session)

            # Fetch bets that arrived since last poll
            try:
                conn = _connect_ro(user_id)
                rows = conn.execute(
                    """SELECT id, timestamp, amount, multiplier_target, result_value,
                              result_display, state, profit, balance_after
                       FROM bets
                       WHERE session_id = ? AND id > ?
                       ORDER BY id ASC""",
                    (session_id, last_bet_id),
                ).fetchall()
                conn.close()
            except Exception:
                rows = []

            for r in rows:
                row = dict(r)
                last_bet_id = row["id"]
                ts = row.get("timestamp") or ""
                try:
                    from datetime import datetime as _dt
                    unix_ts = int(_dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:
                    import time
                    unix_ts = int(time.time())
                chart_point = {
                    "time": unix_ts,
                    "value": round(row.get("balance_after") or 0.0, 8),
                    "bet_num": row["id"],
                    "amount": round(row.get("amount") or 0.0, 8),
                    "profit": round(row.get("profit") or 0.0, 8),
                    "state": row.get("state") or "loss",
                    "target": round(row.get("multiplier_target") or 0.0, 4),
                    "result": row.get("result_display") or str(round(row.get("result_value") or 0.0, 4)),
                }
                await manager.send_to(ws, "new_bet", chart_point)

            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws, channel)
