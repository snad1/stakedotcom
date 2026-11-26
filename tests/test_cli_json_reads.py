"""`--lanes-status --json` and `--stats --json [--lane K] [--before ID]`: the two reads the
Telegram lane commands parse, in WolfBet's shape.

Reference: wolfbet/tests/test_phase11g_json_outputs.py, test_phase6d_reset_and_datestats.py.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

import stake as go

SESSION_KEYS = {"id", "lane", "started_at", "ended_at", "total_bets", "wins", "losses",
                "wagered", "profit", "currency", "start_balance", "end_balance",
                "strategy", "multiplier", "base_bet"}


def _doc(capsys) -> dict:
    out = capsys.readouterr().out
    assert out.count("\n") == 1, out
    return json.loads(out)


def _lanes(count):
    Path(go.CONFIG_PATH).write_text(json.dumps({"lanes": {"count": count}}))


def _state(lane, **fields):
    Path(go.lane_path(go._BASE_STATE_PATH, lane)).write_text(json.dumps(fields))


def test_lanes_json_lists_running_paused_and_never_run_lanes(monkeypatch, capsys):
    monkeypatch.setattr(go.state, "access_token", "sk-live-0123456789abcdef")
    _lanes(3)
    _state(1, pid=os.getpid(), session_id=7, profit=0.5, key_ref="default", currency="usdt")
    _state(2, pid=os.getpid(), session_id=8, profit=-0.1, pause_remaining_sec=30)
    go.cmd_lanes_status(as_json=True)
    doc = _doc(capsys)
    assert doc["v"] == 1 and set(doc) == {"v", "lanes"}
    rows = {r["lane"]: r for r in doc["lanes"]}
    assert (rows[1]["alive"], rows[1]["paused"], rows[1]["state"]["key_ref"]) == (True, False, "default")
    assert (rows[2]["alive"], rows[2]["paused"]) == (True, True)
    assert (rows[3]["alive"], rows[3]["paused"], rows[3]["state"]) == (False, False, None)
    assert "sk-live-0123456789abcdef" not in json.dumps(doc)


def _seed():
    go.init_db()
    conn = sqlite3.connect(go.DB_PATH)
    for i in range(1, 31):
        conn.execute("INSERT INTO sessions (started_at, currency, strategy, base_bet, multiplier, "
                     "total_bets, wins, losses, profit, wagered, lane) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (f"2026-11-01T00:{i:02d}:00", "usdt", "martingale", 0.001, 2.0,
                      10, 6, 4, 0.01, 0.1, 2 if i % 2 else 1))
    conn.commit()
    conn.close()


def test_stats_json_pages_one_lane_back_from_an_id(capsys):
    _seed()
    go.cmd_stats(lane=2, as_json=True, before=20)
    doc = _doc(capsys)
    ids = [s["id"] for s in doc["sessions"]]
    assert ids == sorted((i for i in range(1, 20) if i % 2), reverse=True)
    assert all(set(s) == SESSION_KEYS for s in doc["sessions"])
    assert doc["sessions"][0]["lane"] == 2 and doc["sessions"][0]["currency"] == "USDT"


def test_stats_json_keeps_to_the_last_twenty(capsys):
    _seed()
    go.cmd_stats(as_json=True)
    ids = [s["id"] for s in _doc(capsys)["sessions"]]
    assert ids == list(range(30, 10, -1))


def test_stats_json_without_a_database_says_so_and_creates_none(capsys):
    go.cmd_stats(as_json=True)
    assert _doc(capsys) == {"v": 1, "error": {"code": "no_database", "message": "No database found."}}
    assert not os.path.exists(go.DB_PATH)


@pytest.mark.parametrize("kwargs", [{"lane": 0}, {"since": "not-a-date"}])
def test_stats_json_refuses_a_bad_window(kwargs, capsys):
    _seed()
    go.cmd_stats(as_json=True, **kwargs)
    assert _doc(capsys)["error"]["code"] == "invalid_window"


def test_the_stats_flag_with_json_prints_only_the_document(monkeypatch, capsys):
    _seed()
    monkeypatch.setattr(go.console, "_file", go.console._file)  # main() points it at stderr
    monkeypatch.setattr(sys, "argv", ["stake.py", "--stats", "--json", "--lane", "1", "--before", "5"])
    go.main()
    assert [s["id"] for s in _doc(capsys)["sessions"]] == [4, 2]


@pytest.mark.parametrize("argv", [["--json"], ["--status", "--json"], ["--stats", "--before", "5"]])
def test_json_and_before_outside_their_reads_exit_2(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["stake.py", *argv])
    with pytest.raises(SystemExit) as e:
        go.main()
    assert e.value.code == 2
