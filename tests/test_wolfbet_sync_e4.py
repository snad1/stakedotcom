"""E4-S3 — stakedotcom CLI stats and analytics (WolfBet v2.23.0 .. v2.35.0).

--stats / --analytics window (time-of-day fix, validation), wins / losses per
session, time span vs bet time, the analytics report, the --lane filter and
"By lane". Modelled on WolfBet test_phase8_analytics, 6d (stats part), 6e,
9c, 9d and 10_lanes (stats / analytics lane filter). Seeded temp DB, no
casino traffic.

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q
"""

import inspect
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

import stake as st


def _seed(*rows):
    """Insert sessions rows: dicts with started/ended plus optional fields."""
    st.init_db()
    conn = sqlite3.connect(st.DB_PATH)
    for i, r in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at, currency, game, strategy, "
            "base_bet, multiplier, total_bets, wins, losses, profit, wagered, lane) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, r["started"], r.get("ended"), "usdt", r.get("game", "limbo"),
             r.get("strategy", "Flat Bet"), 0.0001, 2.0,
             r.get("wins", 1) + r.get("losses", 1), r.get("wins", 1), r.get("losses", 1),
             r.get("profit", 0.0), r.get("wagered", 0.01), r.get("lane", 1)))
    conn.commit()
    conn.close()


def _out(capsys, fn, **kw):
    fn(**kw)
    return capsys.readouterr().out


# ── window (v2.30.9 / v2.33.0) ────────────────────────────
def test_since_with_time_of_day_excludes_earlier_session(capsys):
    _seed({"started": "2026-11-13T13:00:00", "ended": "2026-11-13T13:30:00"},
          {"started": "2026-11-13T14:30:00", "ended": "2026-11-13T15:00:00"})
    out = _out(capsys, st.cmd_stats, since="2026-11-13 14:00")
    assert "Session #2" in out
    assert "Session #1" not in out
    assert "Range Totals" in out


def test_bare_until_date_is_inclusive(capsys):
    _seed({"started": "2026-11-13T23:59:30", "ended": "2026-11-13T23:59:50"},
          {"started": "2026-11-14T00:00:10", "ended": "2026-11-14T00:01:00"})
    out = _out(capsys, st.cmd_stats, until="2026-11-13")
    assert "Session #1" in out and "Session #2" not in out


@pytest.mark.parametrize("cmd", [st.cmd_stats, st.cmd_analytics])
def test_invalid_bound_prints_error_and_no_rows(cmd, capsys):
    _seed({"started": "2026-11-13T10:00:00", "ended": "2026-11-13T11:00:00"})
    out = _out(capsys, cmd, since="13/11/2026")
    assert 'Invalid --since/--until "13/11/2026"' in out
    assert "Session #1" not in out and "Totals" not in out and "analytics" not in out


@pytest.mark.parametrize("cmd", [st.cmd_stats, st.cmd_analytics])
def test_no_database_message(cmd, capsys):
    out = _out(capsys, cmd)
    assert "No database found" in out


# ── totals (v2.33.1 / v2.34.0) ────────────────────────────
def test_wins_losses_per_session_window_aware(capsys):
    _seed({"started": "2026-11-10T10:00:00", "ended": "2026-11-10T11:00:00", "wins": 50, "losses": 1},
          {"started": "2026-11-13T10:00:00", "ended": "2026-11-13T11:00:00", "wins": 4, "losses": 6},
          {"started": "2026-11-13T12:00:00", "ended": "2026-11-13T13:00:00", "wins": 2, "losses": 3})
    out = _out(capsys, st.cmd_stats, since="2026-11-13")
    assert "Wins/Session:  Max 4  Avg 3.0" in out
    assert "Losses/Session: Max 6  Avg 4.5" in out

    out = _out(capsys, st.cmd_stats)
    assert "Wins/Session:  Max 50  Avg 18.7" in out


def test_time_span_vs_bet_time_with_pause_gap(capsys):
    # two 1 h sessions with a 2 h pause between them
    _seed({"started": "2026-11-13T10:00:00", "ended": "2026-11-13T11:00:00"},
          {"started": "2026-11-13T13:00:00", "ended": "2026-11-13T14:00:00"})
    out = _out(capsys, st.cmd_stats)
    assert "Time Span:     4h 0m 0s" in out
    assert "Bet Time: 2h 0m 0s" in out
    assert "(2026-11-13 10:00:00 -> 2026-11-13 14:00:00)" in out


def test_running_session_ends_now():
    start = (datetime.now() - timedelta(seconds=90)).isoformat()
    _seed({"started": start, "ended": None})
    conn = sqlite3.connect(st.DB_PATH)
    try:
        _, _, span, bet = st.sessions_time_span(conn)
    finally:
        conn.close()
    assert bet == pytest.approx(90, abs=3) and span == pytest.approx(90, abs=3)


# ── analytics (v2.23.0 / v2.31.0) ─────────────────────────
def _seed_mix():
    _seed({"started": "2026-11-12T10:00:00", "ended": "2026-11-12T11:00:00",
           "game": "limbo", "strategy": "Martingale", "profit": 0.5, "wins": 6, "losses": 4},
          {"started": "2026-11-12T12:00:00", "ended": "2026-11-12T12:30:00",
           "game": "dice", "strategy": "Flat Bet", "profit": -0.2, "wins": 2, "losses": 8},
          {"started": "2026-11-13T10:00:00", "ended": "2026-11-13T10:10:00",
           "game": "dice", "strategy": "Martingale", "profit": 0.1, "wins": 5, "losses": 5})


def test_analytics_overall_and_breakdowns(capsys):
    _seed_mix()
    out = _out(capsys, st.cmd_analytics)
    assert "Lifetime analytics" in out and "3 sessions" in out and "30 bets" in out
    assert "Sessions in profit:  2" in out
    assert "Sessions in loss:    1" in out
    assert "+0.40000000" in out                     # total P/L
    assert "Best session:" in out and "+0.50000000" in out
    assert "-0.20000000" in out
    assert "Time range:" in out and "2026-11-12 10:00:00 -> 2026-11-13 10:10:00" in out
    assert "Time span:" in out and "1d 0h 10m 0s" in out
    assert "Bet time:" in out and "1h 40m 0s" in out
    assert "By game" in out and "Limbo" in out and "Dice" in out
    assert "By strategy" in out and "Martingale" in out and "Flat Bet" in out
    assert "Top 5 best sessions" in out and "Top 5 worst sessions" in out
    assert "By lane" not in out                     # one lane: table hidden


def test_analytics_group_and_top_order():
    _seed_mix()
    games = st._analytics_by_game()
    assert [(g["game"], g["sessions"]) for g in games] == [("Limbo", 1), ("Dice", 2)]
    strats = st._analytics_by_strategy()
    assert strats[0]["strategy"] == "Martingale" and strats[0]["profit"] == pytest.approx(0.6)
    best = st._analytics_top_sessions("profit_desc", 5)
    worst = st._analytics_top_sessions("profit_asc", 5)
    assert [r["id"] for r in best] == [1, 3, 2]
    assert [r["id"] for r in worst] == [2, 3, 1]


def test_analytics_window(capsys):
    _seed_mix()
    out = _out(capsys, st.cmd_analytics, since="2026-11-13")
    assert "Range analytics" in out and "1 sessions" in out
    assert "[2026-11-13 → …]" in out


def test_analytics_empty_db(capsys):
    st.init_db()
    out = _out(capsys, st.cmd_analytics)
    assert "No sessions in the DB yet" in out


# ── lanes (v2.35.0) ───────────────────────────────────────
def _seed_lanes():
    _seed({"started": "2026-11-13T10:00:00", "ended": "2026-11-13T11:00:00", "lane": 1, "profit": 0.3},
          {"started": "2026-11-13T10:00:02", "ended": "2026-11-13T11:00:00", "lane": 2, "profit": -0.1},
          {"started": "2026-11-13T12:00:00", "ended": "2026-11-13T12:30:00", "lane": 2, "profit": 0.2})


def test_stats_lane_filter_and_tag(capsys):
    _seed_lanes()
    out = _out(capsys, st.cmd_stats, lane=2)
    assert "Session #2" in out and "Session #3" in out and "Session #1" not in out
    assert "[L2]" in out
    assert "[lane 2]" in out and "Range Totals" in out

    out = _out(capsys, st.cmd_stats)
    assert "Session #1 " in out              # lane 1: no tag
    assert "Session #1 [L1]" not in out


def test_analytics_lane_filter_and_by_lane(capsys):
    _seed_lanes()
    rows = st._analytics_by_lane()
    assert [(r["lane"], r["sessions"]) for r in rows] == [("Lane 1", 1), ("Lane 2", 2)]
    assert rows[1]["profit"] == pytest.approx(0.1)

    out = _out(capsys, st.cmd_analytics)
    assert "By lane" in out and "Lane 2" in out

    out = _out(capsys, st.cmd_analytics, lane=2)
    assert "2 sessions" in out and "By lane" not in out


def test_init_db_adds_lane_to_old_db_idempotent():
    conn = sqlite3.connect(st.DB_PATH)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT)")
    conn.execute("INSERT INTO sessions VALUES (1, '2026-11-01T10:00:00', '2026-11-01T11:00:00')")
    conn.commit()
    conn.close()
    st.init_db()
    st.init_db()
    conn = sqlite3.connect(st.DB_PATH)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        assert cols.count("lane") == 1
        assert conn.execute("SELECT lane FROM sessions").fetchone() == (1,)
    finally:
        conn.close()


# ── argparse ──────────────────────────────────────────────
def test_main_dispatches_stats_and_analytics_with_lane(monkeypatch):
    calls = []
    monkeypatch.setattr(st, "cmd_stats", lambda **kw: calls.append(("stats", kw)))
    monkeypatch.setattr(st, "cmd_analytics", lambda **kw: calls.append(("analytics", kw)))
    monkeypatch.setattr(sys, "argv", ["stake.py", "--stats", "--since", "2026-11-13 14:00", "--lane", "2"])
    st.main()
    monkeypatch.setattr(sys, "argv", ["stake.py", "--analytics", "--until", "2026-11-13"])
    st.main()
    assert calls == [
        ("stats", {"since": "2026-11-13 14:00", "until": None, "lane": 2}),
        ("analytics", {"since": None, "until": "2026-11-13", "lane": None}),
    ]


def test_lane_out_of_range_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["stake.py", "--stats", "--lane", "11"])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert e.value.code == 2
    assert "--lane must be 1-10" in capsys.readouterr().out


# ── Run header (item 5, shipped in 1.10.0) ────────────────
def test_monitor_run_segment_absent_for_old_state_file():
    src = inspect.getsource(st._build_monitor_screen)
    assert 'if d.get("run_uptime_sec") is not None else ""' in src
