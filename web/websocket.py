"""WebSocket manager — delegates to shared library."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.web.websocket import ConnectionManager, manager, make_authenticate_ws  # noqa: E402

from .auth import decode_token, COOKIE_NAME  # noqa: E402

authenticate_ws = make_authenticate_ws(decode_token, COOKIE_NAME)
