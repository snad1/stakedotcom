"""Each session records the vault key it bet with, and the lane state file names that key.

Reference: wolfbet/tests/test_phase10_lanes.py (session key, older database, state key).
"""

import json
import sqlite3
from pathlib import Path

import stake as go


def _keys(*ids):
    conn = sqlite3.connect(go.DB_PATH)
    rows = dict(conn.execute(
        f"SELECT id, key_ref FROM sessions WHERE id IN ({','.join('?' * len(ids))})", ids))
    conn.close()
    return rows


def test_a_session_records_the_key_its_lane_bets_with(monkeypatch):
    go.init_db()
    monkeypatch.setattr(go.state, "key_ref", "friend-a")
    own = go._db_start_session()
    monkeypatch.setattr(go.state, "key_ref", None)
    main = go._db_start_session()
    assert _keys(own, main) == {own: "friend-a", main: "default"}


def test_an_older_database_gains_the_key_column_once():
    conn = sqlite3.connect(go.DB_PATH)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at TEXT)")
    conn.execute("INSERT INTO sessions (started_at) VALUES ('2026-11-01T00:00:00')")
    conn.commit()
    conn.close()
    go.init_db()
    go.init_db()
    conn = sqlite3.connect(go.DB_PATH)
    assert conn.execute("SELECT key_ref FROM sessions").fetchall() == [(None,)]
    conn.close()


def test_the_state_file_names_the_lane_key_but_never_the_secret(monkeypatch):
    monkeypatch.setattr(go.state, "key_ref", "friend-a")
    monkeypatch.setattr(go.state, "access_token", "sk-live-0123456789abcdef")
    go._save_state_file()
    raw = Path(go.STATE_PATH).read_text()
    snap = json.loads(raw)
    assert snap["key_ref"] == "friend-a"
    assert "access_token" not in snap and "sk-live-0123456789abcdef" not in raw
