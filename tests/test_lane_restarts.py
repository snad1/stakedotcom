"""What a stake setting change costs, what a lane still waits on, and session-end restarts.

Reference: wolfbet/tests/test_phase16_pending_restart.py, test_phase16_lane_drain.py,
test_phase16_rolling_update.py and shuffle/tests/test_lane_restarts.py.
"""

import json
import os
import sys
from pathlib import Path

import pytest

import stake as go

TOKEN = "stake-main-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def _lanes(monkeypatch, tmp_path):
    monkeypatch.setattr(go, "_BASE_STATE_PATH", go.STATE_PATH)
    monkeypatch.setattr(go, "_BASE_PID_PATH", go.PID_PATH)
    monkeypatch.setattr(go, "DRAIN_PATH", str(tmp_path / "drain"))
    monkeypatch.setattr(go, "_log_tag", go._log_tag)
    monkeypatch.setattr(go.console, "width", 200)
    fmt = go._log_handler.formatter
    yield
    go._log_handler.setFormatter(fmt)


def _write(**extra) -> Path:
    data = {"access_token": TOKEN, "lockdown_token": "lockdown-main-0123",
            "cookie": "session=main-abc", "currency": "usdt", "game": "limbo", "base_bet": 0.01,
            "multiplier_target": 2.0, "strategy_key": "1",
            "lanes": {"split": "none", "next_id": 4, "items": {
                str(k): {"name": f"Lane {k}", "enabled": True, "overrides": {}} for k in (1, 2, 3)}},
            **extra}
    path = Path(go.CONFIG_PATH)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return path


def _load(lane=1):
    go.state = go.BotState()
    go.state.lane = lane
    assert go.load_config()
    return go.state


def _set(pairs) -> int:
    """stake's config-set exits on a refusal and returns on success: give one exit code."""
    try:
        go.cmd_config_set(pairs)
    except SystemExit as e:
        return e.code
    return 0


# ── the settings table ──────────────────────────────────────────────────

def test_every_setting_has_one_class():
    keys = set(go._CONFIG_KEYS) | set(go._STREAK_TUPLE_KEYS)
    assert set(go._SETTINGS) == keys
    assert {apply for apply, _ in go._SETTINGS.values()} == {"live", "lane", "service"}
    assert go._SETTINGS["max_profit"] == ("live", True)
    assert go._SETTINGS["strategy"] == ("lane", True)
    assert go._SETTINGS["cookie"] == ("service", False)
    assert go._SETTINGS["db_write_mode"] == ("live", False)


def test_the_derived_lists_are_what_they_were():
    assert go._LIVE_RELOAD_KEYS == {
        "base_bet", "basebet_pct", "bet_delay", "custom_rules_text", "db_write_mode",
        "max_bets", "max_loss", "max_profit", "max_wins",
        "milestone_bets", "milestone_losses", "milestone_profit", "milestone_wins",
        "profit_increment", "profit_threshold", "proxy", "purge_days", "recurring",
        "recurring_delay", "stop_on_balance", "streak_delay_bets", "streak_delay_loss",
        "streak_delay_win", "streak_reset_delay", "streak_reset_losses", "streakbet_loss"}
    assert go._LANE_FORBIDDEN_KEYS == {"access_token", "lockdown_token", "cookie", "game",
                                       "purge_days", "db_write_mode"}


def test_a_base_restart_only_change_names_every_lane(capsys):
    _write()
    assert _set(["mult=3"]) == 0
    assert "Restart every lane to apply: multiplier_target (stakectl restart)." in capsys.readouterr().out
    assert _set(["maxloss=1"]) == 0
    assert "Restart every lane" not in capsys.readouterr().out


def test_a_lane_restart_only_change_names_that_lane(capsys):
    _write()
    assert _set(["lane2.loss_mult=3"]) == 0
    assert "Restart lane 2 to apply: loss_mult (stakectl restart 2)." in capsys.readouterr().out
    assert _set(["lane2.maxloss=1"]) == 0
    assert "Restart lane 2" not in capsys.readouterr().out
    assert _set(["lane2.loss_mult=default"]) == 0
    assert "Restart lane 2 to apply: loss_mult" in capsys.readouterr().out


# ── what a running lane still waits on ──────────────────────────────────

def _snapshot() -> dict:
    """What this lane writes to its state file."""
    go._save_state_file()
    path = Path(go.STATE_PATH)
    data = json.loads(path.read_text())
    path.unlink()
    return data


def _save_running(state, lane, pid=None):
    data = _snapshot()
    data["pid"] = pid or os.getpid()
    Path(go.lane_path(go._BASE_STATE_PATH, lane)).write_text(json.dumps(data))


def test_a_restart_only_change_waits_and_a_live_one_applies(capsys):
    path = _write()
    s = _load(2)
    data = json.loads(path.read_text())
    data["multiplier_target"], data["max_loss"] = 3.0, 0.5
    path.write_text(json.dumps(data))
    go._reload_live_config()
    assert s.max_loss == 0.5
    assert s.pending_restart == {"multiplier_target": [2.0, 3.0]}
    _save_running(s, 2)
    go.cmd_lanes_status()
    out = capsys.readouterr().out
    assert "Lane 2 waits for a restart: multiplier_target" in out and "(stakectl restart 2)" in out
    data["multiplier_target"] = 2.0
    path.write_text(json.dumps(data))
    go._reload_live_config()
    assert s.pending_restart == {}


def test_a_stopped_lane_lists_nothing_and_the_token_never(capsys):
    path = _write()
    s = _load(2)
    data = json.loads(path.read_text())
    data["access_token"], data["cookie"], data["strategy"] = "other-token-0123456789ab", "c=2", "X"
    path.write_text(json.dumps(data))
    go._reload_live_config()
    assert not go._PENDING_HIDDEN & set(s.pending_restart) and "strategy" in s.pending_restart
    _save_running(s, 2, pid=2 ** 22 + 12345)
    go.cmd_lanes_status()
    assert "waits for a restart" not in capsys.readouterr().out


def test_the_code_column_says_which_code_each_lane_runs(capsys):
    _write()
    s = _load(2)
    assert _snapshot()["code_version"] == go.VERSION
    assert go._lane_code(2, {"code_version": go.VERSION}) == "new"
    assert go._lane_code(2, {"code_version": "1.0.0"}) == "old"
    go.cmd_lane_drain(2, "restart")
    assert go._lane_code(2, {"code_version": "1.0.0"}) == "waiting for session end"
    _save_running(s, 2)
    capsys.readouterr()
    go.cmd_lanes_status()
    out = capsys.readouterr().out
    assert "Code" in out and "new" in out


# ── session-end restart and removal ─────────────────────────────────────

def _flag(lane):
    return go.lane_path(go.DRAIN_PATH, lane)


def test_a_request_is_written_privately_and_cancelled(capsys):
    _write()
    assert go.cmd_lane_drain(2, "restart") == 0
    assert "Lane 2 restarts when its session ends. It keeps betting until then." in capsys.readouterr().out
    assert open(_flag(2)).read() == "restart" and oct(os.stat(_flag(2)).st_mode & 0o777) == "0o600"
    assert go.cmd_lane_drain(2, "cancel") == 0
    assert "Lane 2 no longer waits for its session end." in capsys.readouterr().out
    assert not os.path.exists(_flag(2))


@pytest.mark.parametrize(("lane", "action", "message"), [
    (9, "restart", "No lane 9."), (1, "remove", "Lane 1 holds the base config and can't be removed.")])
def test_bad_requests_write_nothing(capsys, lane, action, message):
    _write()
    assert go.cmd_lane_drain(lane, action) == 2
    assert message in capsys.readouterr().out and not os.path.exists(_flag(lane))


@pytest.fixture
def flushed(monkeypatch):
    import subprocess
    calls = {"exec": [], "popen": [], "flushed": []}
    monkeypatch.setattr(go.os, "execv", lambda exe, argv: calls["exec"].append(argv))
    monkeypatch.setattr(go, "_stop_bet_writer", lambda timeout=5.0: calls["flushed"].append("writer"))
    monkeypatch.setattr(go, "_save_state_file", lambda: calls["flushed"].append("state"))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls["popen"].append(argv))
    go.state.lane = 2
    return calls


def test_no_request_means_the_session_carries_on(flushed):
    assert go._session_boundary(final=False) is False
    assert flushed == {"exec": [], "popen": [], "flushed": []}


def test_a_restart_flushes_then_re_executes_the_same_command(flushed):
    _write()
    go.cmd_lane_drain(2, "restart")
    go._session_boundary(final=False)
    assert flushed["flushed"] == ["writer", "state"]
    assert flushed["exec"] == [[sys.executable] + sys.argv]
    assert not os.path.exists(_flag(2))


def test_with_recurring_off_a_restart_is_dropped(flushed):
    _write()
    go.cmd_lane_drain(2, "restart")
    assert go._session_boundary(final=True) is False
    assert flushed["exec"] == [] and not os.path.exists(_flag(2))


def test_a_removal_hands_the_lane_to_stakectl_and_stops_it(flushed):
    _write()
    for final in (False, True):
        go.cmd_lane_drain(2, "remove")
        assert go._session_boundary(final=final) is True
    assert flushed["popen"] == [[go.STAKECTL, "lane", "remove", "2"]] * 2


def test_another_lanes_request_is_left_alone(flushed):
    _write()
    go.cmd_lane_drain(3, "restart")
    assert go._session_boundary(final=False) is False
    assert os.path.exists(_flag(3)) and flushed["exec"] == []


class _Reexec(Exception):
    """Stands in for os.execv, which would replace the test process."""


def test_the_loop_restarts_at_the_session_end_and_not_before(fake_casino, cli_state, monkeypatch):
    fake_casino.queue("win", "loss", "win")
    s = cli_state(max_bets=3, recurring=True)
    with open(go.lane_path(go.DRAIN_PATH, 1), "w") as f:
        f.write("restart")

    def reexec(exe, argv):
        raise _Reexec

    monkeypatch.setattr(go.os, "execv", reexec)
    with pytest.raises(_Reexec):
        go.betting_loop()
    assert s.total_bets == 3


def test_with_recurring_off_the_loop_stops_as_before(fake_casino, cli_state, monkeypatch):
    fake_casino.queue("win", "loss", "win")
    s = cli_state(max_bets=3)
    with open(go.lane_path(go.DRAIN_PATH, 1), "w") as f:
        f.write("restart")
    monkeypatch.setattr(go.os, "execv", lambda exe, argv: pytest.fail("must not restart"))
    go.betting_loop()
    assert s.total_bets == 3 and s.running is False
