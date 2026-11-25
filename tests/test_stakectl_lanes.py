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
