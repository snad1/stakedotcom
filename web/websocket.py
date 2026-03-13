"""WebSocket manager for real-time updates."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from .auth import decode_token

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections grouped by channel."""

    def __init__(self):
        self._channels: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, channel: str):
        await ws.accept()
        async with self._lock:
            if channel not in self._channels:
                self._channels[channel] = set()
            self._channels[channel].add(ws)
        logger.debug("WS connected: %s (total=%d)", channel, len(self._channels.get(channel, [])))

    async def disconnect(self, ws: WebSocket, channel: str):
        async with self._lock:
            if channel in self._channels:
                self._channels[channel].discard(ws)
                if not self._channels[channel]:
                    del self._channels[channel]

    async def broadcast(self, channel: str, msg_type: str, data: dict):
        message = json.dumps({
            "type": msg_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat(),
        })
        async with self._lock:
            sockets = list(self._channels.get(channel, []))
        dead = []
        for ws in sockets:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws, channel)

    async def send_to(self, ws: WebSocket, msg_type: str, data: dict):
        message = json.dumps({
            "type": msg_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat(),
        })
        await ws.send_text(message)

    def has_subscribers(self, channel: str) -> bool:
        return bool(self._channels.get(channel))

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    @property
    def connection_count(self) -> int:
        return sum(len(s) for s in self._channels.values())


manager = ConnectionManager()


async def authenticate_ws(ws: WebSocket) -> Optional[dict]:
    """Validate JWT from query param or cookie on WS connect."""
    token = ws.query_params.get("token")
    if not token:
        # Fall back to httpOnly cookie from headers
        from .auth import COOKIE_NAME
        token = ws.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return decode_token(token)
