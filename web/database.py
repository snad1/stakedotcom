"""Web app's own SQLite database — admin users, sessions, audit log."""

import sqlite3
import uuid
from datetime import datetime
from passlib.hash import bcrypt

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT DEFAULT 'admin',
    created_at    TEXT DEFAULT (datetime('now')),
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS tg_users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    tier          TEXT DEFAULT 'free',
    first_seen    TEXT DEFAULT (datetime('now')),
    last_active   TEXT,
    is_blocked    INTEGER DEFAULT 0,
    notes         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS web_sessions (
    id            TEXT PRIMARY KEY,
    user_type     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now')),
    expires_at    TEXT NOT NULL,
    revoked       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT DEFAULT (datetime('now')),
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    detail        TEXT DEFAULT ''
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(_SCHEMA)

    # Seed default admin if not exists
    row = conn.execute(
        "SELECT id FROM admin_users WHERE username = ?",
        (settings.admin_user,),
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, ?)",
            (settings.admin_user, bcrypt.hash(settings.admin_pass), "admin"),
        )
    conn.commit()
    conn.close()


# ── Admin user helpers ──

def verify_admin(username: str, password: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM admin_users WHERE username = ?", (username,)
    ).fetchone()
    if row and bcrypt.verify(password, row["password_hash"]):
        conn.execute(
            "UPDATE admin_users SET last_login = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), row["id"]),
        )
        conn.commit()
        conn.close()
        return dict(row)
    conn.close()
    return None


# ── TG user helpers ──

def sync_tg_user(user_id: int, username: str = None, tier: str = None):
    conn = get_db()
    existing = conn.execute(
        "SELECT user_id FROM tg_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    now = datetime.utcnow().isoformat()
    if existing:
        updates = ["last_active = ?"]
        params = [now]
        if username:
            updates.append("username = ?")
            params.append(username)
        if tier:
            updates.append("tier = ?")
            params.append(tier)
        params.append(user_id)
        conn.execute(
            f"UPDATE tg_users SET {', '.join(updates)} WHERE user_id = ?",
            params,
        )
    else:
        conn.execute(
            "INSERT INTO tg_users (user_id, username, tier, first_seen, last_active) VALUES (?, ?, ?, ?, ?)",
            (user_id, username or "", tier or "free", now, now),
        )
    conn.commit()
    conn.close()


def get_tg_users() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tg_users ORDER BY last_active DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tg_user(user_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM tg_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_tg_user(user_id: int, **kwargs):
    conn = get_db()
    sets = [f"{k} = ?" for k in kwargs]
    vals = list(kwargs.values()) + [user_id]
    conn.execute(f"UPDATE tg_users SET {', '.join(sets)} WHERE user_id = ?", vals)
    conn.commit()
    conn.close()


# ── Audit log ──

def audit(actor: str, action: str, detail: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (actor, action, detail) VALUES (?, ?, ?)",
        (actor, action, detail),
    )
    conn.commit()
    conn.close()


def get_audit_log(limit: int = 100) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Web session tracking ──

def create_web_session(user_type: str, user_id: str, expires_at: str) -> str:
    jti = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO web_sessions (id, user_type, user_id, expires_at) VALUES (?, ?, ?, ?)",
        (jti, user_type, user_id, expires_at),
    )
    conn.commit()
    conn.close()
    return jti


def is_session_valid(jti: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT revoked FROM web_sessions WHERE id = ?", (jti,)
    ).fetchone()
    conn.close()
    return row is not None and not row["revoked"]


def revoke_session(jti: str):
    conn = get_db()
    conn.execute("UPDATE web_sessions SET revoked = 1 WHERE id = ?", (jti,))
    conn.commit()
    conn.close()
