"""stakectl v2.3: one lane is added, removed, renamed, stopped, started or restarted alone.

Reference: wolfbet/tests/test_phase11i_wolfctl_verbs.py. The stub bot writes
config-after.json over the config when asked, so the lane delta is the one stake.py writes.
"""

import json

from tests.test_wolfbet_sync_e7 import (  # noqa: F401  # env is a fixture
    _after, _config, _ctl, _log, _units, env,
)


def _running(env, *units):
    d = env["home"] / "units"
    d.mkdir(exist_ok=True)
    for u in units:
        (d / f"{u}.active").touch()
        (d / f"{u}.enabled").touch()


def _registry_after(env, items):
    (env["home"] / "config-after.json").write_text(json.dumps(
        {"base_bet": 0.001, "lanes": {"split": "none", "next_id": max(items) + 1,
                                      "items": {str(k): {"name": n, "enabled": True, "overrides": {}}
                                                for k, n in items.items()}}}))


def _no_restarts(env):
    log = _log(env, "systemctl.log")
    return "restart stake\n" not in log and "stop stake\n" not in log


def test_lane_add_starts_only_the_new_lane(env):
    _config(env, lanes=2)
    _running(env, "stake", "stake-lane@2")
    _registry_after(env, {1: "Lane 1", 2: "Lane 2", 3: "friend-A"})
    res = _ctl(env, "lane", "add", "friend-A", "--from", "2")
    assert res.returncode == 0, res.stderr
    assert "--lane-add friend-A --lane-from 2" in _log(env, "py.log")
    assert "stake-lane@3.active" in _units(env)
    assert "lane 3 started" in res.stdout and "Other lanes keep betting." in res.stdout
    assert _no_restarts(env) and "stop stake-lane@2" not in _log(env, "systemctl.log")


def test_lane_remove_disables_only_that_lane(env):
    _config(env, lanes=3)
    _running(env, "stake", "stake-lane@2", "stake-lane@3")
    res = _ctl(env, "lane", "remove", "3")
    assert res.returncode == 0, res.stderr
    assert "--lane-remove 3" in _log(env, "py.log")
    assert "Lane 3 stopped. Other lanes keep betting." in res.stdout
    assert {"stake-lane@2.active", "stake.active"} <= _units(env)
    assert "stake-lane@3.active" not in _units(env) and _no_restarts(env)


def test_lane_rename_writes_the_name_through_the_bot(env):
    _config(env, lanes=3)
    res = _ctl(env, "lane", "rename", "3", "night-run")
    assert res.returncode == 0, res.stderr
    assert "--config-set lane3.name=night-run" in _log(env, "py.log")
    assert _ctl(env, "lane", "rename", "7", "x").returncode == 2


def test_one_lane_stops_starts_and_restarts_alone(env):
    _config(env, lanes=3)
    _running(env, "stake", "stake-lane@2", "stake-lane@3")
    res = _ctl(env, "restart", "2")
    assert "Lane 2 restarted. Other lanes keep betting." in res.stdout
    res = _ctl(env, "stop", "1")
    assert "Lane 1 stopped. Other lanes keep betting." in res.stdout
    assert {"stake-lane@2.active", "stake-lane@3.active"} <= _units(env)
    assert "stake.active" not in _units(env)
    assert "Lane 1 started." in _ctl(env, "start", "1").stdout
    log = _log(env, "systemctl.log")
    assert "stop stake-lane@3" not in log and "restart stake-lane@3" not in log


def test_an_unknown_lane_reaches_no_unit(env):
    _config(env, lanes=3)
    res = _ctl(env, "restart", "4")
    assert res.returncode == 2 and "No lane 4: this bot runs 3 lane(s)." in res.stderr
    assert "restart stake-lane@4" not in _log(env, "systemctl.log")


def test_bad_lane_arguments_exit_2(env):
    _config(env, lanes=2)
    for args in (("lane", "add"), ("lane", "add", "x", "--from", "y"), ("lane", "remove", "x"),
                 ("lane", "rename", "2"), ("lane", "explode"), ("stop", "x")):
        assert _ctl(env, *args).returncode == 2, args
    assert "--lane-" not in _log(env, "py.log")


def test_lanes_max_only_sets_the_limit(env):
    _config(env, lanes=2)
    _running(env, "stake", "stake-lane@2")
    res = _ctl(env, "lanes", "--max", "5")
    assert res.returncode == 0 and "Lane limit set to 5." in res.stdout
    assert "--config-set lanes_max=5" in _log(env, "py.log")
    assert "stake-lane@2.active" in _units(env)


TOKEN = "stake-friend-token-fedcba9876543210"
LOCKDOWN = "lockdown-0123456789"
COOKIE = "session=abc; cf_clearance=xyz-123"


def test_key_add_sends_the_three_account_secrets_on_stdin_only(env):
    res = _ctl(env, "key", "add", "friend-a", "--label", "Friend A",
               stdin=f"{TOKEN}\n{LOCKDOWN}\n{COOKIE}\n")
    assert res.returncode == 0, res.stderr
    log = _log(env, "py.log")
    assert "--key-put friend-a --key-kind account --key-label Friend A" in log
    for secret in (TOKEN, LOCKDOWN, COOKIE):
        assert secret not in log and secret not in res.stdout and secret not in res.stderr
    assert _log(env, "stdin.txt") == f"{TOKEN}\n{LOCKDOWN}\n{COOKIE}\n"


def test_key_add_of_a_proxy_or_telegram_sends_one_line(env):
    assert _ctl(env, "key", "add", "px", "--kind", "proxy", stdin="socks5://u:p@1.2.3.4:1080\n").returncode == 0
    assert _log(env, "stdin.txt") == "socks5://u:p@1.2.3.4:1080\n"
    assert "--key-put px --key-kind proxy" in _log(env, "py.log")


def test_key_rotate_list_and_delete_reach_the_bot(env):
    assert _ctl(env, "key", "rotate", "friend-a", stdin=f"{TOKEN}\n{LOCKDOWN}\n{COOKIE}\n").returncode == 0
    assert _log(env, "stdin.txt") == f"{TOKEN}\n{LOCKDOWN}\n{COOKIE}\n"
    assert _ctl(env, "key", "list").returncode == 0
    assert _ctl(env, "key", "delete", "friend-a").returncode == 0
    log = _log(env, "py.log")
    assert "--key-rotate friend-a --key-kind account" in log
    assert "--key-list" in log and "--key-delete friend-a" in log
    assert TOKEN not in log


def test_bad_key_arguments_exit_2_and_reach_no_bot(env):
    for args in (("key",), ("key", "add"), ("key", "add", "x", "--kind", "cookie"),
                 ("key", "rotate", "x", "--label", "y"), ("key", "delete"), ("key", "list", "x")):
        assert _ctl(env, *args, stdin=TOKEN + "\n").returncode == 2, args
    assert "--key-" not in _log(env, "py.log")


def test_restart_at_the_boundary_asks_the_bot_and_touches_no_unit(env):
    _config(env, lanes=3)
    _running(env, "stake", "stake-lane@2", "stake-lane@3")
    assert _ctl(env, "restart", "2", "--at-boundary").returncode == 0
    assert _ctl(env, "restart", "2", "--cancel-boundary").returncode == 0
    assert _ctl(env, "lane", "remove", "3", "--after-session").returncode == 0
    log = _log(env, "py.log")
    assert "--lane-drain 2 restart" in log and "--lane-drain 2 cancel" in log
    assert "--lane-drain 3 remove" in log
    assert "restart stake-lane@2" not in _log(env, "systemctl.log") and _no_restarts(env)
    assert "stake-lane@3.active" in _units(env)
    for args in (("restart", "x", "--at-boundary"), ("restart", "2", "--soon"),
                 ("restart", "2", "--at-boundary", "x")):
        assert _ctl(env, *args).returncode == 2, args


def _checkout(env, tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "stake.py").write_text("VERSION = 'new'\n")
    (repo / "requirements.txt").write_text("")
    return repo


def test_an_update_asks_each_running_lane_to_restart_at_its_session_end(env, tmp_path):
    _config(env, lanes=3)
    _running(env, "stake", "stake-lane@3")
    res = _ctl(env, "update", cwd=_checkout(env, tmp_path))
    assert res.returncode == 0, res.stderr
    assert "Lane 1 restarts on the new code when its session ends." in res.stdout
    assert "Lane 3 restarts on the new code when its session ends." in res.stdout
    assert "Lane 2 restarts" not in res.stdout
    assert "Betting goes on; use stakectl update --critical to restart every lane now." in res.stdout
    drains = [line for line in _log(env, "py.log").splitlines() if "--lane-drain" in line]
    assert [d.split("stake.py ")[1] for d in drains] == ["--lane-drain 1 restart",
                                                          "--lane-drain 3 restart"]
    assert _no_restarts(env)


def test_a_critical_update_restarts_every_lane_now(env, tmp_path):
    _config(env, lanes=2)
    _running(env, "stake", "stake-lane@2")
    res = _ctl(env, "update", "--critical", cwd=_checkout(env, tmp_path))
    assert res.returncode == 0, res.stderr
    assert "--user restart stake" in _log(env, "systemctl.log")
    assert "--lane-drain" not in _log(env, "py.log") and "Betting goes on" not in res.stdout


def test_an_update_of_a_stopped_bot_asks_no_lane(env, tmp_path):
    _config(env, lanes=2)
    res = _ctl(env, "update", cwd=_checkout(env, tmp_path))
    assert res.returncode == 0 and "CLI bot not running: stakectl start" in res.stdout
    assert "--lane-drain" not in _log(env, "py.log")


def test_an_unknown_update_option_exits_2(env):
    res = _ctl(env, "update", "--soon")
    assert res.returncode == 2 and "Usage: stakectl update [--critical]" in res.stderr


def _lane_files(k):
    return [f".stake_autobot_live.lane{k}.json", f".stake_autobot.lane{k}.pid", f".stake_run.lane{k}.json",
            f".stake_autobot.lane{k}.drain"]


def _touch(env, names):
    for name in names:
        (env["home"] / name).write_text("{}")


def _gone(env, names):
    return not any((env["home"] / n).exists() for n in names)


def test_removing_a_stopped_lane_at_session_end_removes_it_now(env):
    _config(env, lanes=3)
    _running(env, "stake", "stake-lane@2")
    _touch(env, _lane_files(3) + [".stake_autobot_live.lane2.json"])
    res = _ctl(env, "lane", "remove", "3", "--after-session")
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines()[0] == "Lane 3 is not running, so it is removed now."
    assert "Lane 3 stopped. Other lanes keep betting." in res.stdout
    log = _log(env, "py.log")
    assert "--lane-remove 3" in log and "--lane-drain" not in log
    assert _gone(env, _lane_files(3))
    assert (env["home"] / ".stake_autobot_live.lane2.json").exists()


def test_removing_a_lane_now_clears_its_files(env):
    _config(env, lanes=2)
    _running(env, "stake", "stake-lane@2")
    _touch(env, _lane_files(2))
    res = _ctl(env, "lane", "remove", "2")
    assert res.returncode == 0, res.stderr
    assert _gone(env, _lane_files(2))


def test_a_refused_remove_keeps_the_lane_files(env):
    _config(env, lanes=2)
    py = env["home"] / "stake-bot" / "venv" / "bin" / "python3"
    py.write_text(py.read_text().replace('echo "$*" >> "$HOME/py.log"\n',
                                         'echo "$*" >> "$HOME/py.log"\n'
                                         '[[ "$*" == *--lane-remove* ]] && { echo "No lane 5."; exit 2; }\n'))
    _touch(env, [".stake_autobot_live.lane2.json"])
    res = _ctl(env, "lane", "remove", "2")
    assert res.returncode == 2
    assert (env["home"] / ".stake_autobot_live.lane2.json").exists()
    assert "disable --now stake-lane@2" not in _log(env, "systemctl.log")


def test_restarting_a_stopped_lane_at_session_end_says_to_start_it(env):
    _config(env, lanes=2)
    res = _ctl(env, "restart", "2", "--at-boundary")
    assert res.returncode == 1
    assert "Lane 2 is stopped; start it with: stakectl start 2" in res.stderr
    assert "--lane-drain" not in _log(env, "py.log")


def test_cancelling_a_session_end_restart_works_on_a_stopped_lane(env):
    _config(env, lanes=2)
    res = _ctl(env, "restart", "2", "--cancel-boundary")
    assert res.returncode == 0, res.stderr
    assert "--lane-drain 2 cancel" in _log(env, "py.log")


def test_reset_moves_the_wal_with_the_database_and_drops_session_end_requests(env):
    _config(env)
    h = env["home"]
    (h / ".stake_autobot.db").write_text("db")
    left = [".stake_autobot.db-wal", ".stake_autobot.db-shm", ".stake_autobot.lane3.drain", ".stake_autobot.drain"]
    _touch(env, left)
    res = _ctl(env, "reset", "--yes")
    assert res.returncode == 0, res.stderr
    archive = next(h.glob(".stake_autobot-*.db")).name
    assert (h / f"{archive}-wal").exists() and (h / f"{archive}-shm").exists()
    assert _gone(env, left) and not (h / ".stake_autobot.db").exists()


def test_restarting_lane_one_also_brings_up_lanes_that_are_not_running(env):
    _config(env, lanes=3)
    _running(env, "stake", "stake-lane@2")
    res = _ctl(env, "restart", "1")
    assert res.returncode == 0, res.stderr
    log = _log(env, "systemctl.log")
    assert "restart stake\n" in log and "enable --now stake-lane@3" in log
    assert "stake-lane@3.active" in _units(env)
    assert "restart stake-lane@2" not in log and "stop stake-lane@2" not in log


def test_stopping_lane_one_touches_no_other_lane(env):
    _config(env, lanes=2)
    _running(env, "stake")
    res = _ctl(env, "stop", "1")
    assert res.returncode == 0, res.stderr
    assert "stake-lane@2" not in _log(env, "systemctl.log")


def test_rolling_restart_asks_each_running_lane_and_restarts_none_now(env):
    _config(env, lanes=3)
    _running(env, "stake", "stake-lane@2")
    res = _ctl(env, "restart", "--rolling")
    assert res.returncode == 0, res.stderr
    assert "Lane 1 restarts on its saved config when its session ends." in res.stdout
    assert "Lane 2 restarts on its saved config when its session ends." in res.stdout
    assert "Lane 3 is not running; start it with: stakectl start 3" in res.stdout
    assert "Every other lane keeps betting until its own session ends." in res.stdout
    drains = [l.split("--lane-drain ")[1] for l in _log(env, "py.log").splitlines() if "--lane-drain" in l]
    assert drains == ["1 restart", "2 restart"]
    log = _log(env, "systemctl.log")
    assert not any(f" {v} " in f" {l} " for l in log.splitlines()
                   for v in ("restart", "stop", "start", "enable", "disable"))


def test_rolling_restart_needs_a_running_bot(env):
    _config(env, lanes=2)
    res = _ctl(env, "restart", "--rolling")
    assert res.returncode == 1
    assert "The CLI bot is not running; start it with: stakectl start" in res.stderr
    assert "--lane-drain" not in _log(env, "py.log")
    assert _ctl(env, "restart", "--rolling", "x").returncode == 2


def test_lanes_json_asks_the_bot_for_one_document(env):
    _config(env, lanes=2)
    assert _ctl(env, "lanes", "--json").returncode == 0
    assert "--lanes-status --json" in _log(env, "py.log")
    res = _ctl(env, "lanes", "3", "--json")
    assert res.returncode == 2 and "--json works with 'stakectl lanes' alone" in res.stderr


def test_stats_json_passes_the_lane_and_the_page_through(env):
    _config(env)
    assert _ctl(env, "stats", "--json", "--lane", "2", "--before", "5").returncode == 0
    assert "--stats --json --lane 2 --before 5" in _log(env, "py.log")
