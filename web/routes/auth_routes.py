"""Authentication routes — delegates to shared library."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from ..config import settings, TEMPLATES_DIR
from ..auth import create_token, COOKIE_NAME, decode_token
from ..database import verify_admin, create_web_session, revoke_session, audit
from shared.web.routes.auth_routes import make_auth_router  # noqa: E402

router = make_auth_router(
    templates_dir=TEMPLATES_DIR,
    settings=settings,
    create_token=create_token,
    decode_token=decode_token,
    cookie_name=COOKIE_NAME,
    verify_admin=verify_admin,
    create_web_session=create_web_session,
    revoke_session=revoke_session,
    audit=audit,
)
