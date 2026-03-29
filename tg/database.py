"""Telegram per-user file paths — delegates to shared library."""

import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from .config import DATA_DIR
from core.database import init_db, db_connect  # noqa: F401 — re-exported
from shared.tg.database import (  # noqa: E402
    user_dir as _user_dir,
    user_db_path as _user_db_path,
    user_config_path as _user_config_path,
    user_presets_path as _user_presets_path,
    load_user_config as _load_user_config,
    save_user_config as _save_user_config,
    load_presets as _load_presets,
    save_presets as _save_presets,
)

DB_FILENAME = "stake.db"

user_dir = lambda user_id: _user_dir(DATA_DIR, user_id)
user_db_path = lambda user_id: _user_db_path(DATA_DIR, DB_FILENAME, user_id)
user_config_path = lambda user_id: _user_config_path(DATA_DIR, user_id)
user_presets_path = lambda user_id: _user_presets_path(DATA_DIR, user_id)
load_user_config = lambda user_id: _load_user_config(DATA_DIR, user_id)
save_user_config = lambda user_id, config: _save_user_config(DATA_DIR, user_id, config)
load_presets = lambda user_id: _load_presets(DATA_DIR, user_id)
save_presets = lambda user_id, presets: _save_presets(DATA_DIR, user_id, presets)


def user_cf_cache_path(user_id: int) -> str:
    return os.path.join(user_dir(user_id), "cf_cookies.json")
