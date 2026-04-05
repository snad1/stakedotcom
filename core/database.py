"""Database schema, migrations, and connection helpers.

Single source of truth for the sessions/bets DB schema.
Both the CLI bot and Telegram bot use this.
"""

import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.core.database import (  # noqa: E402
    SESSIONS_SCHEMA,
    cleanup_old_bets, cleanup_live_bets, db_connect, secure_path,
)


def init_db(db_path: str):
    """Initialize the SQLite database with sessions and bets tables."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(SESSIONS_SCHEMA)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER,
            timestamp    TEXT,
            game         TEXT,
            amount       REAL,
            multiplier_target REAL,
            result_value REAL,
            result_display TEXT,
            state        TEXT,
            profit       REAL,
            balance_after REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    for tbl, col, defn in [
        ("bets", "game", "TEXT DEFAULT 'limbo'"),
        ("bets", "result_display", "TEXT DEFAULT ''"),
        ("sessions", "config_snapshot", "TEXT DEFAULT ''"),
        ("sessions", "chart_snapshots", "TEXT DEFAULT '[]'"),
    ]:
        try:
            c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
