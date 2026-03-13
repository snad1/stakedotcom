"""Read-only access to per-user Stake bot SQLite databases."""

import json
import os
import sqlite3
from typing import Optional

from .config import settings

DB_FILENAME = "stake.db"
CONFIG_FILENAME = "config.json"


def _user_db_path(user_id: int) -> str:
    return os.path.join(settings.bot_data_dir, str(user_id), DB_FILENAME)


def _user_config_path(user_id: int) -> str:
    return os.path.join(settings.bot_data_dir, str(user_id), CONFIG_FILENAME)


def _connect_ro(user_id: int) -> sqlite3.Connection:
    """Read-only connection to a user's bot database (WAL-safe)."""
    path = _user_db_path(user_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"No database for user {user_id}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ── User discovery ──

def discover_users() -> list[int]:
    """Scan bot data dir for numeric user directories."""
    if not os.path.isdir(settings.bot_data_dir):
        return []
    users = []
    for name in os.listdir(settings.bot_data_dir):
        if name.isdigit():
            db_path = os.path.join(settings.bot_data_dir, name, DB_FILENAME)
            if os.path.exists(db_path):
                users.append(int(name))
    return sorted(users)


# ── User config ──

def get_user_config(user_id: int) -> dict:
    path = _user_config_path(user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_user_config(user_id: int, config: dict):
    path = _user_config_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


# ── Sessions ──

def get_sessions(user_id: int, limit: int = 50) -> list[dict]:
    try:
        conn = _connect_ro(user_id)
    except FileNotFoundError:
        return []
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session(user_id: int, session_id: int) -> Optional[dict]:
    try:
        conn = _connect_ro(user_id)
    except FileNotFoundError:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_session_stats(user_id: int) -> dict:
    """Aggregate stats across all sessions for a user."""
    try:
        conn = _connect_ro(user_id)
    except FileNotFoundError:
        return {"total_sessions": 0, "total_bets": 0, "total_profit": 0, "total_wagered": 0}
    row = conn.execute("""
        SELECT
            COUNT(*) as total_sessions,
            COALESCE(SUM(total_bets), 0) as total_bets,
            COALESCE(SUM(profit), 0) as total_profit,
            COALESCE(SUM(wagered), 0) as total_wagered,
            MAX(highest_balance) as peak_balance,
            MAX(highest_win) as best_win,
            MAX(biggest_loss) as worst_loss,
            MAX(max_win_streak) as best_win_streak,
            MAX(max_loss_streak) as worst_loss_streak
        FROM sessions
    """).fetchone()
    conn.close()
    return dict(row) if row else {}


# ── Bets ──

def get_bets(user_id: int, session_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
    try:
        conn = _connect_ro(user_id)
    except FileNotFoundError:
        return []
    rows = conn.execute(
        "SELECT * FROM bets WHERE session_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (session_id, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_bet_count(user_id: int, session_id: int) -> int:
    try:
        conn = _connect_ro(user_id)
    except FileNotFoundError:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM bets WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ── Aggregate stats across all users ──

def get_platform_stats() -> dict:
    """Aggregate stats across all users for the admin dashboard."""
    users = discover_users()
    total_sessions = 0
    total_bets = 0
    total_profit = 0.0
    total_wagered = 0.0
    active_users = 0

    for uid in users:
        stats = get_session_stats(uid)
        if stats.get("total_sessions", 0) > 0:
            active_users += 1
            total_sessions += stats.get("total_sessions", 0)
            total_bets += stats.get("total_bets", 0)
            total_profit += stats.get("total_profit", 0)
            total_wagered += stats.get("total_wagered", 0)

    return {
        "total_users": len(users),
        "active_users": active_users,
        "total_sessions": total_sessions,
        "total_bets": total_bets,
        "total_profit": total_profit,
        "total_wagered": total_wagered,
    }
