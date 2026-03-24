"""Shared database schema, migrations, and connection helpers.

Single source of truth for the sessions/bets DB schema.
Both the CLI bot and Telegram bot use this.
"""

import os
import sqlite3


def init_db(db_path: str):
    """Initialize the SQLite database with sessions and bets tables."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    TEXT,
            ended_at      TEXT,
            currency      TEXT,
            game          TEXT,
            strategy      TEXT,
            base_bet      REAL,
            multiplier    REAL,
            total_bets    INTEGER DEFAULT 0,
            wins          INTEGER DEFAULT 0,
            losses        INTEGER DEFAULT 0,
            profit        REAL    DEFAULT 0,
            wagered       REAL    DEFAULT 0,
            start_balance REAL,
            end_balance   REAL,
            max_win_streak  INTEGER DEFAULT 0,
            max_loss_streak INTEGER DEFAULT 0,
            highest_balance REAL DEFAULT 0,
            lowest_balance  REAL DEFAULT 0,
            highest_win     REAL DEFAULT 0,
            biggest_loss    REAL DEFAULT 0,
            bets_per_minute REAL DEFAULT 0,
            bets_per_second REAL DEFAULT 0,
            peak_bps        REAL DEFAULT 0,
            low_bps         REAL DEFAULT 0,
            peak_bpm        REAL DEFAULT 0,
            low_bpm         REAL DEFAULT 0
        )
    """)
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
    # Migrate: add columns if missing
    for tbl, col, defn in [
        ("bets", "game", "TEXT DEFAULT 'limbo'"),
        ("bets", "result_display", "TEXT DEFAULT ''"),
        ("sessions", "config_snapshot", "TEXT DEFAULT ''"),
    ]:
        try:
            c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def cleanup_old_bets(db_path: str, bet_age_days: int = 3):
    """Delete bet rows for ended sessions older than bet_age_days.

    Session rows (with all pre-aggregated stats) are kept forever.
    Only the individual bet detail rows are purged to reclaim disk space.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    c = conn.cursor()

    # Migrate: add bets_purged column if missing
    try:
        c.execute("ALTER TABLE sessions ADD COLUMN bets_purged INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    c.execute("""
        DELETE FROM bets
        WHERE session_id IN (
            SELECT id FROM sessions
            WHERE ended_at IS NOT NULL
              AND ended_at < datetime('now', ?)
              AND bets_purged = 0
        )
    """, (f'-{bet_age_days} days',))

    deleted = c.rowcount

    if deleted > 0:
        c.execute("""
            UPDATE sessions SET bets_purged = 1
            WHERE ended_at IS NOT NULL
              AND ended_at < datetime('now', ?)
              AND bets_purged = 0
        """, (f'-{bet_age_days} days',))

    conn.commit()

    if deleted > 0:
        conn.execute("VACUUM")

    conn.close()
    return deleted


def db_connect(db_path: str):
    """Create a WAL-mode connection to the database."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def secure_path(path: str):
    """Set restrictive file permissions (owner read/write only)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
