"""JWT authentication — delegates to shared library."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.web.auth import make_auth  # noqa: E402

from .config import settings  # noqa: E402
from .database import is_session_valid, revoke_session  # noqa: E402

COOKIE_NAME = "stake_admin_token"

_auth = make_auth(
    secret_key=settings.secret_key,
    algorithm=settings.jwt_algorithm,
    expire_hours=settings.jwt_expire_hours,
    cookie_name=COOKIE_NAME,
    is_session_valid_fn=is_session_valid,
    revoke_session_fn=revoke_session,
)

create_token = _auth["create_token"]
decode_token = _auth["decode_token"]
get_token_from_request = _auth["get_token_from_request"]
get_current_admin = _auth["get_current_admin"]
get_current_tg_user = _auth["get_current_tg_user"]
get_current_user = _auth["get_current_user"]
get_optional_user = _auth["get_optional_user"]
