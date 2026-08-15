"""Portability port v1 — locks the wolfbet v2.27-v2.30 hardening subset.

Run: APP_ENV=testing python3 -m pytest stake/tests/ -q
"""

import inspect
import os
import re
import sqlite3
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_CASINO = os.path.dirname(_ROOT)
if _CASINO not in sys.path:
    sys.path.insert(0, _CASINO)

os.environ.setdefault("APP_ENV", "testing")

import stake as st  # noqa: E402
from tg import engine as tg_engine, handlers as tg_handlers, bot as tg_bot  # noqa: E402
import tg  # noqa: E402


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s).replace("\x1b[K", "")


def _mk_db():
    fd, path = tempfile.mkstemp(prefix="st_", suffix=".db")
    os.close(fd)
    os.remove(path)
    from core.database import init_db
    init_db(path)
    return path


def _mk_engine(db):
    return tg_engine.BettingEngine(
        user_id=1, db_path=db,
        config={"base_bet": 0.0001, "strategy": "Flat Bet", "strategy_key": "1"},
    )


# ── VERSION ───────────────────────────────────────────────
def test_cli_version():
    assert st.VERSION == "1.9.0"


def test_tg_version():
    assert tg.VERSION == "1.11.0"


# ── PRAGMAs ───────────────────────────────────────────────
def test_tg_get_conn_applies_all_pragmas():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        conn = e._get_conn()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -32000
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
    finally:
        e._db_connection = None
        os.remove(db)


def test_cli_db_conn_source_has_pragmas():
    src = inspect.getsource(st._db_conn)
    assert "synchronous = NORMAL" in src
    assert "busy_timeout = 5000" in src
    assert "cache_size = -32000" in src
    assert "temp_store = MEMORY" in src


# ── Restart=on-failure ────────────────────────────────────
def test_install_st_restart_on_failure():
    with open(os.path.join(_ROOT, "install.sh")) as f:
        src = f.read()
    assert src.count("Restart=on-failure") >= 2
    assert "Restart=always" not in src


# ── Uptime pause bookkeeping ──────────────────────────────
def test_frest_engine_has_no_frozen_timestamps():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        assert e.session_bet_ended_at is None
        assert e.session_pause_started_at is None
    finally:
        os.remove(db)


def test_pause_freezes_and_resume_clears():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time() - 300
        e.pause()
        assert e.session_bet_ended_at is not None
        assert e.session_pause_started_at is not None
        e.resume()
        assert e.session_bet_ended_at is None
        assert e.session_pause_started_at is None
    finally:
        os.remove(db)


def test_get_status_freezes_uptime_when_paused():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time() - 600
        e.session_bet_ended_at = time.time() - 300
        e._status_cache = None
        st = e.get_status()
        assert 299 <= st["uptime_sec"] <= 301
    finally:
        os.remove(db)


def test_get_status_has_new_keys():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time()
        e._status_cache = None
        st = e.get_status()
        assert "uptime_sec" in st
        assert "pause_remaining_sec" in st
    finally:
        os.remove(db)


# ── DB ended_at freeze ────────────────────────────────────
def test_db_save_session_freezes_ended_at():
    db = _mk_db()
    try:
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO sessions (id, started_at, currency, game, strategy, "
                     "base_bet, multiplier, start_balance) VALUES (?,?,?,?,?,?,?,?)",
                     (1, "2026-07-19T09:00:00", "usdt", "dice", "Flat", 0.0001, 2.0, 1.0))
        conn.commit()
        conn.close()
        e = _mk_engine(db)
        e.session_id = 1
        e.session_bet_ended_at = time.time() - 3500
        e._db_save_session(final=True)
        conn = sqlite3.connect(db)
        ended_at_str = conn.execute("SELECT ended_at FROM sessions WHERE id=1").fetchone()[0]
        conn.close()
        e._db_connection = None
        from datetime import datetime as _dt
        delta = time.time() - _dt.fromisoformat(ended_at_str).timestamp()
        assert 3400 < delta < 3600
    finally:
        os.remove(db)


# ── Row 6 config-progress row ─────────────────────────────
def _row_base(**kw):
    d = {
        "profit": 0.0, "profit_threshold": None, "profit_increment": None,
        "last_profit_milestone": 0.0,
        "max_profit": None, "max_loss": None, "max_bets": None, "max_wins": None,
        "stop_on_balance": None, "recurring": False, "recurring_delay_sec": 0,
        "total_bets": 0, "wins": 0, "current_balance": 1.0,
    }
    d.update(kw); return d


def test_row6_fallback_when_no_config():
    row = st._build_config_progress_row(_row_base())
    plain = _strip_ansi(row)
    assert "CFG" in plain
    assert "no goals set" in plain


def test_row6_is_single_line():
    row = st._build_config_progress_row(_row_base(max_profit=0.01))
    assert "\n" not in row


def test_row6_max_profit_yellow_at_85():
    d = _row_base(max_profit=0.01, profit=0.0085)
    row = st._build_config_progress_row(d)
    assert "\x1b[33m" in row
    assert "(85%)" in _strip_ansi(row)


def test_row6_max_profit_bold_red_at_96():
    d = _row_base(max_profit=0.01, profit=0.0096)
    assert "\x1b[1;31m" in st._build_config_progress_row(d)


def test_row6_max_bets_progress():
    plain = _strip_ansi(st._build_config_progress_row(
        _row_base(max_bets=10000, total_bets=4222)))
    assert "MaxB" in plain and "4222/10000" in plain and "(42%)" in plain


def test_row6_recurring_on():
    plain = _strip_ansi(st._build_config_progress_row(
        _row_base(recurring=True, recurring_delay_sec=60)))
    assert "Rec" in plain and "on(60s)" in plain


def test_row6_used_in_monitor():
    src = inspect.getsource(st._build_monitor_screen)
    assert "_build_config_progress_row" in src


# ── TG /diagnose + /analytics ─────────────────────────────
def test_cmd_diagnose_exists():
    assert callable(tg_handlers.cmd_diagnose)


def test_cmd_analytics_exists():
    assert callable(tg_handlers.cmd_analytics)


def test_commands_registered_in_bot():
    src = inspect.getsource(tg_bot)
    assert 'CommandHandler("diagnose"' in src
    assert 'CommandHandler("analytics"' in src


def test_help_mentions_new_commands():
    src = inspect.getsource(tg_handlers.cmd_help)
    assert "/diagnose" in src
    assert "/analytics" in src


# ── Date-range stats (v1.9.0 / TG v1.11.0) ───────────────
def test_stats_date_window_unit():
    clause, params, label = st._stats_date_window()
    assert clause == "" and params == [] and label == ""

    clause, params, label = st._stats_date_window(since="2026-08-01")
    assert "started_at >= ?" in clause
    assert params == ["2026-08-01"]
    assert "2026-08-01" in label

    _, params, _ = st._stats_date_window(until="2026-08-15")
    assert params == ["2026-08-15T23:59:59"]

    _, params, _ = st._stats_date_window(until="2026-08-15T06:00:00")
    assert params == ["2026-08-15T06:00:00"]


def _seed_stats_db(monkeypatch, tmp_path):
    fd, db = tempfile.mkstemp(prefix="st_stats_", suffix=".db")
    os.close(fd); os.remove(db)
    monkeypatch.setattr(st, "DB_PATH", db)
    from core.database import init_db
    init_db(db)
    conn = sqlite3.connect(db)
    for i, started in enumerate(
        ["2026-08-01T10:00:00", "2026-08-10T12:00:00", "2026-08-15T20:00:00"], 1
    ):
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, "
            "base_bet, multiplier, total_bets, wins, losses, profit, wagered) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, started, "usdt", "limbo", "Flat", 0.0001, 2.0,
             100 * i, 50 * i, 50 * i, 0.001 * i, 0.01 * i))
    conn.commit(); conn.close()
    return db


def test_stats_since_filters_listing_and_totals(monkeypatch, tmp_path, capsys):
    db = _seed_stats_db(monkeypatch, tmp_path)
    try:
        st.cmd_stats(since="2026-08-10")
        out = capsys.readouterr().out
        assert "Session #3" in out and "Session #2" in out
        assert "Session #1" not in out
        assert "Range Totals" in out
        assert "2026-08-10" in out
    finally:
        os.remove(db)


def test_stats_window_bare_until_inclusive_and_combined(monkeypatch, tmp_path, capsys):
    db = _seed_stats_db(monkeypatch, tmp_path)
    try:
        st.cmd_stats(until="2026-08-15")
        out = capsys.readouterr().out
        assert "Session #3" in out and "Session #1" in out

        capsys.readouterr()
        st.cmd_stats(since="2026-08-02", until="2026-08-15")
        out = capsys.readouterr().out
        assert "Session #2" in out and "Session #3" in out
        assert "Session #1" not in out
    finally:
        os.remove(db)


def test_stats_no_filter_unchanged(monkeypatch, tmp_path, capsys):
    db = _seed_stats_db(monkeypatch, tmp_path)
    try:
        st.cmd_stats()
        out = capsys.readouterr().out
        assert "Session #1" in out and "Session #2" in out and "Session #3" in out
        assert "All-Time Totals" in out
        assert "last 20" in out
    finally:
        os.remove(db)


def test_since_until_are_modifiers_not_commands():
    """--since/--until must sit on the standalone parser, never inside the
    mutually-exclusive group where --stats lives."""
    src = inspect.getsource(st)
    assert re.search(r'parser\.add_argument\("--since"', src)
    assert re.search(r'parser\.add_argument\("--until"', src)
    assert not re.search(r'group\.add_argument\("--since"', src)
    assert not re.search(r'group\.add_argument\("--until"', src)
    assert "cmd_stats(since=args.since, until=args.until)" in src


def test_tg_analytics_threads_where():
    src = inspect.getsource(tg_handlers.cmd_analytics)
    assert "context.args" in src
    assert src.count("{where}") >= 5
    assert "Range' if args_ else 'Lifetime'" in src


def test_stakectl_stats_passes_args():
    with open(os.path.join(_ROOT, "stakectl")) as f:
        src = f.read()
    assert '--stats "$@"' in src
    assert 'cmd_stats "${@:2}"' in src
