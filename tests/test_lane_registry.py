"""Named lanes in stake: one lane is added, removed, renamed or resized; the others keep betting.

Reference: wolfbet/tests/test_phase10_lanes.py and shuffle/tests/test_lane_registry.py.
"""

import json
import logging
import os
import signal
import sys
from pathlib import Path

import pytest

import stake as go


@pytest.fixture(autouse=True)
def _lane_sandbox(monkeypatch):
    monkeypatch.setattr(go, "_BASE_STATE_PATH", go.STATE_PATH)
    monkeypatch.setattr(go, "_BASE_PID_PATH", go.PID_PATH)
    monkeypatch.setattr(go, "_log_tag", go._log_tag)
    fmt = go._log_handler.formatter
    yield
    go._log_handler.setFormatter(fmt)


@pytest.fixture
def config(cli_state):
    """A saved config; `config(lanes={...})` sets its lanes block."""
    cli_state(base_bet=0.01)
    go.state.running = False
    go.save_config()
    path = Path(go.CONFIG_PATH)

    def write(**lanes):
        data = json.loads(path.read_text())
        data["lanes"] = lanes
        path.write_text(json.dumps(data))
        return path
    return write


def _set(pairs) -> int:
    """stake's config-set exits on a refusal and returns on success: give one exit code."""
    try:
        go.cmd_config_set(pairs)
    except SystemExit as e:
        return e.code
    return 0


def _lanes(path) -> dict:
    return json.loads(Path(path).read_text())["lanes"]


def _enabled(path) -> list:
    reg = go.lane_registry(json.loads(Path(path).read_text()))
    return [k for k in sorted(reg) if reg[k]["enabled"]]


def _items(**entries) -> dict:
    return {str(k): {"name": v[0], "enabled": v[1], "overrides": v[2] if len(v) > 2 else {}}
            for k, v in entries.items()}


def test_an_old_count_config_reads_as_lanes_one_to_count(config):
    path = config(count=3, overrides={"2": {"max_loss": 1.5}})
    assert _enabled(path) == [1, 2, 3]
    go.state = go.BotState()
    go.state.lane = 2
    assert go.load_config() and go.state.lanes_count == 3 and go.state.max_loss == 1.5


def test_lanes_n_round_trip_keeps_disabled_lanes_and_never_reuses_ids(config, monkeypatch):
    path = config(count=3, overrides={"3": {"max_loss": 1.0}})
    monkeypatch.setattr(go.os, "kill", lambda pid, sig: None)
    assert _set(["lanes=1"]) == 0
    assert _enabled(path) == [1]
    assert _lanes(path)["items"]["3"]["overrides"] == {"max_loss": 1.0}
    assert _set(["lanes=4"]) == 0
    assert _enabled(path) == [1, 2, 3, 4] and _lanes(path)["next_id"] == 5


def test_the_first_registry_write_keeps_one_private_copy_of_the_old_file(config):
    path = config(count=2)
    old = path.read_text()
    assert _set(["lane2.name=friend-A"]) == 0
    backup = Path(str(path) + ".pre-registry")
    assert backup.read_text() == old and oct(backup.stat().st_mode & 0o777) == "0o600"
    assert _set(["lane2.name=friend-B"]) == 0
    assert backup.read_text() == old


def test_a_rename_reloads_only_that_lane_and_changes_its_log_prefix(config, monkeypatch, capsys):
    path = config(count=3)
    Path(go.lane_path(go.PID_PATH, 2)).write_text("200")
    Path(go.lane_path(go.PID_PATH, 3)).write_text("300")
    kills = []
    monkeypatch.setattr(go.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    assert _set(["lane3.name=night-run"]) == 0
    assert kills == [(300, signal.SIGHUP)]
    assert _lanes(path)["items"]["3"]["name"] == "night-run"
    go._apply_lane(3)
    go._reload_live_config()
    rec = logging.LogRecord("stake", logging.WARNING, __file__, 1, "hello", (), None)
    assert "[L3 night-run] hello" in go._log_handler.format(rec)


def test_a_bad_name_is_refused_and_nothing_is_written(config, monkeypatch, capsys):
    monkeypatch.setattr(go.console, "width", 160)
    path = config(count=2)
    before = path.read_text()
    assert _set(["lane2.name=bad/name"]) == 2
    assert go.LANE_NAME_HELP in capsys.readouterr().out
    assert path.read_text() == before


def test_add_copies_overrides_and_starts_after_the_last_id(config, capsys):
    path = config(items=_items(**{"1": ("Lane 1", True), "2": ("Lane 2", True, {"max_loss": 2.0})}),
                  next_id=3)
    assert go.cmd_lane_add("friend-A", 2) == 0
    assert "Lane 3 (friend-A) added with lane 2's overrides." in capsys.readouterr().out
    assert _lanes(path)["items"]["3"] == {"name": "friend-A", "enabled": True,
                                          "overrides": {"max_loss": 2.0}}
    assert go.cmd_lane_remove(3) == 0
    assert "Lane 3 (friend-A) removed." in capsys.readouterr().out
    assert go.cmd_lane_add("again") == 0
    assert "4" in _lanes(path)["items"] and "3" not in _lanes(path)["items"]


@pytest.mark.parametrize(("call", "message"), [
    (lambda: go.cmd_lane_remove(1), "Lane 1 holds the base config and can't be removed."),
    (lambda: go.cmd_lane_remove(7), "No lane 7."),
    (lambda: go.cmd_lane_add("x;rm"), go.LANE_NAME_HELP),
    (lambda: go.cmd_lane_add("ok", 9), "No lane 9."),
])
def test_refusals_exit_2_and_write_nothing(config, monkeypatch, capsys, call, message):
    monkeypatch.setattr(go.console, "width", 160)
    path = config(count=2)
    before = path.read_text()
    assert call() == 2
    assert message in capsys.readouterr().out
    assert path.read_text() == before


def test_the_owner_limit_caps_enabled_lanes(config, monkeypatch, capsys):
    monkeypatch.setattr(go.console, "width", 160)
    path = config(count=2, max=2)
    before = path.read_text()
    assert go.cmd_lane_add("third") == 2
    out = capsys.readouterr().out
    assert "This bot already runs 2 lane(s), the limit." in out and "stakectl lanes --max N" in out
    assert _set(["lanes=3"]) == 2
    assert path.read_text() == before
    assert _set(["lanes_max=1"]) == 2
    assert _set(["lanes_max=12", "lanes=12"]) == 0
    assert go.lanes_max(json.loads(path.read_text())) == 12 and len(_enabled(path)) == 12


def test_ids_beyond_ten_run(config):
    path = config(items=_items(**{"1": ("Lane 1", True), "15": ("far", True)}), next_id=16)
    go._apply_lane(15)
    assert go.STATE_PATH == go.lane_path(go._BASE_STATE_PATH, 15)
    assert _enabled(path) == [1, 15]


def test_lane_ids_print_enabled_or_all(config, monkeypatch, capsys):
    config(items=_items(**{"1": ("Lane 1", True), "2": ("Lane 2", False), "4": ("x", True)}),
           next_id=5)
    for flag, out in ((["--lane-ids"], "1 4"), (["--lane-ids", "all"], "1 2 4")):
        monkeypatch.setattr(sys, "argv", ["stake.py", *flag])
        go.main()
        assert capsys.readouterr().out.strip() == out


def test_the_lanes_table_names_lanes_once_one_is_named(config, monkeypatch, capsys):
    monkeypatch.setattr(go.console, "width", 160)
    config(items=_items(**{"1": ("Lane 1", True), "2": ("friend-A", True)}), next_id=3)
    for lane in (1, 2):
        Path(go.lane_path(go.STATE_PATH, lane)).write_text(json.dumps(
            {"pid": os.getpid(), "session_id": lane, "profit": 0.0}))
    go.cmd_lanes_status()
    out = capsys.readouterr().out
    assert "Name" in out and "friend-A" in out


def test_run_reset_removes_every_lane_run_file(config):
    config(count=1)
    for lane in (1, 3, 12):
        Path(go.run_path(go.RUN_PATH, lane)).write_text("{}")
    assert go.cmd_run_reset() == 3
    assert not any(Path(go.run_path(go.RUN_PATH, k)).exists() for k in (1, 3, 12))


def test_a_reload_takes_the_new_lane_count(config):
    path = config(count=2, split="bet")
    go.state = go.BotState()
    go.state.lane = 2
    assert go.load_config() and go.state.lanes_count == 2
    data = json.loads(path.read_text())
    data["lanes"]["count"] = 4
    path.write_text(json.dumps(data))
    go._reload_live_config()
    assert go.state.lanes_count == 4


def test_an_older_casino_shared_is_named_instead_of_a_traceback(tmp_path):
    import shutil
    import subprocess

    from shared.core import lanes
    real = os.path.dirname(os.path.dirname(lanes.__file__))
    shutil.copytree(real, tmp_path / "shared", ignore=shutil.ignore_patterns("tests", ".*"))
    with open(tmp_path / "shared" / "core" / "lanes.py", "a") as f:
        f.write("\ndel registry_block\n")
    bot = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stake.py")
    env = {**os.environ, "PYTHONPATH": f"{tmp_path}{os.pathsep}{os.path.dirname(bot)}",
           "APP_ENV": "testing", "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, bot, "--help"], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert r.stderr.strip() == "casino-shared 0.10.0 or newer is needed: run stakectl update"

