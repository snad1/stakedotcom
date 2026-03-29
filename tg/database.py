"""Telegram-specific per-user file paths and config persistence.

DB schema is imported from core.database (single source of truth).
"""

import os
import json

from .config import DATA_DIR
from core.database import init_db, db_connect  # noqa: F401 — re-exported


# ── Per-user file paths ──────────────────────────────────
def user_dir(user_id: int) -> str:
    path = os.path.join(DATA_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def user_db_path(user_id: int) -> str:
    return os.path.join(user_dir(user_id), "stake.db")


def user_config_path(user_id: int) -> str:
    return os.path.join(user_dir(user_id), "config.json")


def user_presets_path(user_id: int) -> str:
    return os.path.join(user_dir(user_id), "presets.json")


def user_cf_cache_path(user_id: int) -> str:
    return os.path.join(user_dir(user_id), "cf_cookies.json")


# ── User config persistence ─────────────────────────────
def load_user_config(user_id: int) -> dict:
    path = user_config_path(user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_user_config(user_id: int, config: dict):
    path = user_config_path(user_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)


# ── Presets persistence ──────────────────────────────────
def load_presets(user_id: int) -> dict:
    path = user_presets_path(user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_presets(user_id: int, presets: dict):
    path = user_presets_path(user_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(presets, f, indent=2)
