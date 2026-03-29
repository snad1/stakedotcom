"""Web database — delegates to shared library, bound to this bot's settings."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from .config import settings
from shared.web.database import (  # noqa: E402
    _hash_password, _verify_password,
    get_db as _get_db, init_db as _init_db,
    verify_admin as _verify_admin, change_admin_password as _change_admin_password,
    sync_tg_user as _sync_tg_user, get_tg_users as _get_tg_users,
    get_tg_user as _get_tg_user, update_tg_user as _update_tg_user,
    audit as _audit, get_audit_log as _get_audit_log,
    create_web_session as _create_web_session,
    is_session_valid as _is_session_valid, revoke_session as _revoke_session,
)

# Bind db_path from settings
get_db = lambda: _get_db(settings.db_path)
init_db = lambda: _init_db(settings.db_path, settings.admin_user, settings.admin_pass)
verify_admin = lambda username, password: _verify_admin(settings.db_path, username, password)
change_admin_password = lambda user_id, old_password, new_password: _change_admin_password(settings.db_path, user_id, old_password, new_password)
sync_tg_user = lambda user_id, username=None, tier=None: _sync_tg_user(settings.db_path, user_id, username, tier)
get_tg_users = lambda: _get_tg_users(settings.db_path)
get_tg_user = lambda user_id: _get_tg_user(settings.db_path, user_id)
update_tg_user = lambda user_id, **kwargs: _update_tg_user(settings.db_path, user_id, **kwargs)
audit = lambda actor, action, detail="": _audit(settings.db_path, actor, action, detail)
get_audit_log = lambda limit=100: _get_audit_log(settings.db_path, limit)
create_web_session = lambda user_type, user_id, expires_at: _create_web_session(settings.db_path, user_type, user_id, expires_at)
is_session_valid = lambda jti: _is_session_valid(settings.db_path, jti)
revoke_session = lambda jti: _revoke_session(settings.db_path, jti)
