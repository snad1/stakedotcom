"""Stakedotcom CLI parallel lanes (CLI v1.13.0).

Mirrors WolfBet v2.35.0 (parallel lanes) and v2.36.0 (`lane_split`). Lane
helpers come from shared.core.lanes. Reference test:
wolfbet/tests/test_phase10_lanes.py; ported shape: goated's lane tests. Stake
extras: `--benchmark --lanes` on GraphQL UserBalances, and the shared CF
cookie cache (atomic write + cross-lane re-auth lock).

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q
"""

import importlib.util
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

import stake as st
from shared.core.session_rules import should_restart

from .fakes import FakeCasino, FakeResponse, GQL, CF_PAGE

FORBIDDEN = ("access_token", "lockdown_token", "cookie", "game", "purge_days", "db_write_mode")


@pytest.fixture(autouse=True)
def _lane_sandbox(monkeypatch):
    """Lane paths derive from the sandboxed base paths; log format restored."""
    monkeypatch.setattr(st, "_BASE_STATE_PATH", st.STATE_PATH)
    monkeypatch.setattr(st, "_BASE_PID_PATH", st.PID_PATH)
    fmt = st._log_handler.formatter
    yield
    st._log_handler.setFormatter(fmt)


@pytest.fixture
def saved_config(cli_state):
    cli_state(base_bet=0.01)
    st.state.running = False
    st.save_config()
    return Path(st.CONFIG_PATH)


def _raw(path) -> dict:
    return json.loads(Path(path).read_text())


def _write_lanes(path, **lanes):
    data = _raw(path)
    data["lanes"] = lanes
    Path(path).write_text(json.dumps(data))


def _config_set(pairs) -> int:
    """stake's cmd_config_set exits on errors; return the exit code (0 = ok)."""
    try:
        st.cmd_config_set(pairs)
    except SystemExit as e:
        return e.code
    return 0


def _fresh_load(lane=1):
    st.state = st.BotState()
    st.state.lane = lane
    assert st.load_config()
    return st.state


# ── L1 identity ────────────────────────────────────────────────────────

def test_apply_lane_switches_paths_and_log_prefix(monkeypatch):
    base_state, base_pid = st.STATE_PATH, st.PID_PATH
    st._apply_lane(3)
    assert st.state.lane == 3
    assert st.STATE_PATH == st.lane_path(base_state, 3) != base_state
    assert st.PID_PATH == st.lane_path(base_pid, 3) != base_pid
    rec = logging.LogRecord("stake", logging.WARNING, __file__, 1, "hello", (), None)
    assert "[L3] hello" in st._log_handler.format(rec)


def test_apply_lane_one_keeps_classic_paths():
    base_state, base_pid = st.STATE_PATH, st.PID_PATH
    fmt = st._log_handler.formatter
    st._apply_lane(None)
    st._apply_lane(1)
    assert (st.STATE_PATH, st.PID_PATH) == (base_state, base_pid)
    assert st._log_handler.formatter is fmt
    assert st.state.lane == 1


@pytest.mark.parametrize("bad", [0, 100, -1])
def test_apply_lane_range(bad):
    with pytest.raises(ValueError):
        st._apply_lane(bad)


def test_main_bad_lane_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["stake.py", "--status", "--lane", "100"])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert e.value.code == 2
    assert "--lane must be 1-99, got 100" in capsys.readouterr().out


def test_status_with_lane_reads_lane_state_file(monkeypatch, capsys):
    lane_state = st.lane_path(st.STATE_PATH, 2)
    Path(lane_state).write_text(json.dumps({"pid": 999999, "session_id": 77}))
    seen = []
    monkeypatch.setattr(st, "cmd_status", lambda: seen.append(st.STATE_PATH))
    monkeypatch.setattr(sys, "argv", ["stake.py", "--status", "--lane", "2"])
    st.main()
    assert seen == [lane_state]


def test_systemd_unit_per_lane():
    assert st._service_unit() == "stake"
    st._apply_lane(4)
    assert st._service_unit() == "stake-lane@4"


# ── L2 DB ──────────────────────────────────────────────────────────────

def test_session_row_tagged_with_lane(cli_state):
    cli_state()
    st.state.lane = 2
    sid = st._db_start_session()
    with sqlite3.connect(st.DB_PATH) as c:
        rows = dict(c.execute("SELECT id, lane FROM sessions").fetchall())
    assert rows[sid] == 2
    assert rows[sid - 1] == 1          # the fixture's lane-1 session


# ── L4 overrides / config ──────────────────────────────────────────────

def test_load_config_applies_lane_overrides(saved_config):
    _write_lanes(saved_config, count=3,
                 overrides={"2": {"max_loss": 1.5, "multiplier_target": 3.0,
                                  "streak_delay_loss": "3:2.5",
                                  "currency": "btc", "proxy": "socks5://10.0.0.1:1080",
                                  "game": "dice"}})
    s = _fresh_load(2)
    assert s.lanes_count == 3
    assert s.max_loss == 1.5 and s.multiplier_target == 3.0
    assert s.streak_delay_loss == (3, 2.5)
    assert s.currency == "btc" and s.proxy == "socks5://10.0.0.1:1080"   # a lane's own account
    assert s.game == "limbo"                                             # forbidden: base wins
    s1 = _fresh_load(1)
    assert s1.max_loss is None and s1.multiplier_target == 2.0


def test_save_config_keeps_lanes_block_and_lane_k_never_writes(saved_config):
    block = {"count": 2, "overrides": {"2": {"max_loss": 1.0}}}
    _write_lanes(saved_config, **block)
    s = _fresh_load(1)
    s.max_profit = 9.0
    st.save_config()
    data = _raw(saved_config)
    assert data["lanes"] == block and data["max_profit"] == 9.0

    before = saved_config.read_text()
    s2 = _fresh_load(2)
    s2.max_profit = 1.0
    st.save_config()
    assert saved_config.read_text() == before


def test_live_reload_merges_lane_overrides(saved_config):
    _fresh_load(2)
    _write_lanes(saved_config, count=2, overrides={"2": {"max_loss": 0.5, "bet_delay": 0.2}})
    applied = st._reload_live_config()
    assert st.state.max_loss == 0.5 and st.state.bet_delay == 0.2
    assert set(applied) == {"max_loss", "bet_delay"}


def test_config_set_lanes_split_and_overrides(saved_config, monkeypatch, capsys):
    Path(st.lane_path(st.PID_PATH, 2)).write_text("4242")
    kills = []
    monkeypatch.setattr(st.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    rc = _config_set(["lanes=3", "lane_split=budget",
                            "lane2.maxloss=1.5", "lane3.mult=3", "lane3.sdloss=4:1"])
    assert rc == 0
    lanes = _raw(saved_config)["lanes"]
    assert st.lanes_count(_raw(saved_config)) == 3 and lanes["split"] == "budget"
    assert lanes["items"]["2"]["overrides"] == {"max_loss": 1.5}
    ov3 = lanes["items"]["3"]["overrides"]
    assert ov3["multiplier_target"] == 3.0 and ov3["streak_delay_loss"] == "4:1.0"
    assert kills == [(4242, signal.SIGHUP)]            # only the touched running lane
    assert "Budget shares update when each lane restarts" in " ".join(capsys.readouterr().out.split())
    # the base config is unchanged
    assert _raw(saved_config)["max_loss"] is None

    assert _config_set(["lane2.maxloss=default"]) == 0
    assert _raw(saved_config)["lanes"]["items"]["2"]["overrides"] == {}


def test_config_set_same_as_base_still_pins_override(saved_config):
    assert _config_set(["lane2.mult=2"]) == 0
    assert _raw(saved_config)["lanes"]["items"]["2"]["overrides"] == {"multiplier_target": 2.0}


@pytest.mark.parametrize("key", list(FORBIDDEN) + ["purgedays", "dbmode"])
def test_config_set_forbidden_lane_key_exits_2(saved_config, key, capsys):
    before = saved_config.read_text()
    assert _config_set([f"lane2.{key}=x"]) == 2
    assert saved_config.read_text() == before


@pytest.mark.parametrize("pairs", [["lanes=0"], ["lanes=11"], ["lanes=x"],
                                   ["lane1.maxloss=1"], ["lane100.maxloss=1"],
                                   ["lane_split=half"], ["lane2.lane_split=bet"],
                                   ["lane2.maxloss=abc"], ["lane2.nope=1"]])
def test_config_set_lane_syntax_refused(saved_config, pairs):
    before = saved_config.read_text()
    assert _config_set(pairs) == 2
    assert saved_config.read_text() == before


def test_config_set_base_signals_every_running_lane(saved_config, monkeypatch):
    _write_lanes(saved_config, count=5)
    Path(st.PID_PATH).write_text("100")
    Path(st.lane_path(st.PID_PATH, 2)).write_text("200")
    Path(st.lane_path(st.PID_PATH, 5)).write_text("500")
    kills = []
    monkeypatch.setattr(st.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    assert _config_set(["maxloss=5"]) == 0
    assert [p for p, _ in kills] == [100, 200, 500]
    assert _raw(saved_config)["max_loss"] == 5.0


def test_config_show_prints_lanes(saved_config, capsys):
    _write_lanes(saved_config, count=3, split="both", overrides={"2": {"max_loss": 1.0}})
    st.cmd_config_show()
    out = capsys.readouterr().out
    assert "Lanes: 3" in out and "split: both" in out
    assert "lane2 overrides: max_loss=1.00000000" in out


# ── v2.36.0 lane_split ─────────────────────────────────────────────────

def test_split_default_none_uses_full_bet(saved_config):
    _write_lanes(saved_config, count=4)
    s = _fresh_load(2)
    assert s.lane_split == "none" and s.base_bet == 0.01


@pytest.mark.parametrize("mode,expected", [("none", 0.01), ("budget", 0.01),
                                           ("bet", 0.0025), ("both", 0.0025)])
def test_split_modes_base_bet(saved_config, mode, expected):
    _write_lanes(saved_config, count=4, split=mode)
    s = _fresh_load(1)
    assert s.base_bet == expected and s.current_bet == expected


def test_split_bet_keeps_lane_base_bet_override(saved_config):
    _write_lanes(saved_config, count=4, split="bet", overrides={"2": {"base_bet": 0.007}})
    assert _fresh_load(2).base_bet == 0.007


def test_split_bet_rounding_to_zero_keeps_full(saved_config):
    data = _raw(saved_config)
    data["base_bet"] = 1e-8
    data["lanes"] = {"count": 4, "split": "bet"}
    saved_config.write_text(json.dumps(data))
    assert _fresh_load(1).base_bet == 1e-8


def test_split_bet_save_config_writes_full_bet(saved_config):
    _write_lanes(saved_config, count=2, split="bet")
    s = _fresh_load(1)
    assert s.base_bet == 0.005
    st.save_config()
    assert _raw(saved_config)["base_bet"] == 0.01
    s.base_bet = 0.02                       # a real change is written as is
    st.save_config()
    assert _raw(saved_config)["base_bet"] == 0.02


def test_split_basebet_pct_divided(cli_state):
    s = cli_state(basebet_pct=0.01, current_balance=10.0, lanes_count=4, split_lanes=4, lane_split="bet")
    assert st._apply_basebet_pct()
    assert s.base_bet == pytest.approx(0.025)


def test_split_live_reload_no_spurious_base_bet_warning(saved_config, caplog):
    _write_lanes(saved_config, count=2, split="bet")
    _fresh_load(2)
    with caplog.at_level(logging.WARNING, logger="stake"):
        st._reload_live_config()
    assert "base_bet" not in caplog.text


# ── L3 split budget ────────────────────────────────────────────────────

def test_init_lane_budget():
    s = st.state
    s.current_balance, s.lanes_count, s.split_lanes, s.lane_split = 9.0, 3, 3, "budget"
    st._init_lane_budget()
    assert s.lane_share == pytest.approx(3.0)
    s.lane_split = "none"
    st._init_lane_budget()
    assert s.lane_share is None
    s.lanes_count, s.split_lanes, s.lane_split = 1, 1, "both"
    st._init_lane_budget()
    assert s.lane_share is None


def test_budget_stop_when_next_bet_exceeds_equity():
    s = st.state
    s.lane_share, s.lane_run_profit, s.profit, s.current_bet = 1.0, -0.2, -0.3, 0.6
    stop, reason = st.should_stop()
    assert stop and reason.startswith("Lane budget exceeded")
    # never a recurring restart, even with recurring on and no loss
    assert not should_restart(reason, 1.0, True, 5)


def test_budget_ok_within_equity_and_without_split():
    s = st.state
    s.lane_share, s.lane_run_profit, s.profit, s.current_bet = 1.0, 0.0, -0.3, 0.7
    assert st.should_stop() == (False, "")
    s.lane_share, s.current_bet = None, 1e9
    assert st.should_stop() == (False, "")


def test_recurring_cycle_carries_lane_profit(cli_state):
    s = cli_state(lane_share=2.0)
    s.profit = 0.4
    s.running = True
    st._reset_session_for_recurring(delay=0)
    assert s.lane_run_profit == pytest.approx(0.4) and s.profit == 0.0
    assert st.lane_equity_now() == pytest.approx(2.4)


def test_risky_config_uses_lane_share_only_with_budget():
    s = st.state
    s.strategy_key, s.loss_mult, s.base_bet = "2", 2.0, 1.0
    s.streak_reset_losses, s.max_loss = 3, None
    s.current_balance, s.lanes_count, s.split_lanes = 64.0, 4, 4

    def chain():
        return next(w for w in st._check_risky_config() if w.startswith("Streak reset"))

    s.lane_split = "none"
    assert "balance covers a 6-loss chain" in chain()
    s.lane_split = "budget"
    assert "lane share covers a 4-loss chain" in chain()


def test_state_file_has_lane_fields(cli_state):
    s = cli_state(lane=2, lanes_count=2, split_lanes=2, lane_split="budget", lane_share=5.0)
    s.lane_run_profit = 0.5
    st._save_state_file()
    d = _raw(st.STATE_PATH)
    assert (d["lane"], d["lanes_count"], d["lane_split"]) == (2, 2, "budget")
    assert d["lane_share"] == 5.0 and d["lane_equity"] == pytest.approx(5.5)


# ── L5 start stagger ───────────────────────────────────────────────────

class _Started(Exception):
    pass


def test_lane_start_stagger_and_budget(cli_state, monkeypatch, sleeps):
    cli_state(lanes_count=3, split_lanes=3, lane_split="budget")
    sleeps.clear()
    monkeypatch.setattr(st, "_load_and_connect", lambda **k: None)
    monkeypatch.setattr(st, "_start_bet_writer", lambda: None)

    def started():
        raise _Started(list(sleeps))
    monkeypatch.setattr(st, "_db_start_session", started)
    monkeypatch.setattr(sys, "argv", ["stake.py", "--daemon", "--lane", "3"])
    with pytest.raises(_Started) as e:
        st.main()
    assert e.value.args[0] == [4.0]
    assert st.state.lane_share == pytest.approx(10.0 / 3)


def test_lane_one_does_not_stagger(cli_state, monkeypatch, sleeps):
    cli_state()
    sleeps.clear()
    monkeypatch.setattr(st, "_load_and_connect", lambda **k: None)
    monkeypatch.setattr(st, "_start_bet_writer", lambda: None)
    monkeypatch.setattr(st, "_db_start_session",
                        lambda: (_ for _ in ()).throw(_Started(list(sleeps))))
    monkeypatch.setattr(sys, "argv", ["stake.py", "--daemon"])
    with pytest.raises(_Started) as e:
        st.main()
    assert e.value.args[0] == []
    assert st.state.lane_share is None


# ── lanes status ───────────────────────────────────────────────────────

def test_lanes_status_table(monkeypatch, capsys):
    import os
    monkeypatch.setattr(st.console, "width", 160)
    Path(st.CONFIG_PATH).write_text(json.dumps({"lanes": {"count": 3}}))
    Path(st.STATE_PATH).write_text(json.dumps(
        {"pid": os.getpid(), "session_id": 1, "profit": 0.5, "bets_per_second": 2.0,
         "uptime_sec": 60, "run_uptime_sec": 120, "lane_equity": None}))
    Path(st.lane_path(st.STATE_PATH, 2)).write_text(json.dumps(
        {"pid": 999999, "session_id": 2, "profit": -0.25, "lane_equity": 4.75}))
    Path(st.lane_path(st.STATE_PATH, 3)).write_text("{bad")
    st.cmd_lanes_status()
    out = capsys.readouterr().out
    assert "Parallel lanes" in out and "LIVE" in out and "STOPPED" in out
    assert "unreadable" in out and "4.75000000" in out
    assert "lanes live 1/3" in out and "2.0 bps" in out


def test_lanes_status_without_files_lists_lane_one_not_running(monkeypatch, capsys):
    monkeypatch.setattr(st.console, "width", 160)
    st.cmd_lanes_status()
    out = capsys.readouterr().out
    assert "NOT RUNNING" in out and "lanes live 0/1" in out


def test_a_lane_that_never_ran_is_listed_not_running(monkeypatch, capsys):
    import os
    monkeypatch.setattr(st.console, "width", 160)
    Path(st.CONFIG_PATH).write_text(json.dumps({"lanes": {"count": 2}}))
    Path(st.STATE_PATH).write_text(json.dumps({"pid": os.getpid(), "session_id": 3, "profit": 0.0}))
    assert [(k, d is None, alive) for k, d, alive in st._read_lane_states()] == [
        (1, False, True), (2, True, False)]
    st.cmd_lanes_status()
    out = capsys.readouterr().out
    assert "NOT RUNNING" in out and "unreadable" not in out and "lanes live 1/2" in out


# ── two lanes on one account ───────────────────────────────────────────

class _LaneCasino(FakeCasino):
    """One lane's scripted outcomes on a balance shared by every lane."""

    def __init__(self, account, script):
        self.account = account
        super().__init__(balance=account["balance"], script=script)

    @property
    def balance(self):
        return self.account["balance"]

    @balance.setter
    def balance(self, value):
        self.account["balance"] = value

    def handle(self, method, url, body):
        with self.account["lock"]:
            return super().handle(method, url, body)


def _load_lane_module(name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, st.__file__)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def _setup_lane(mod, lane, casino, monkeypatch, **kw):
    for name in ("DB_PATH", "CONFIG_PATH"):
        monkeypatch.setattr(mod, name, getattr(st, name))
    monkeypatch.setattr(mod, "_BASE_STATE_PATH", st.STATE_PATH)
    monkeypatch.setattr(mod, "_BASE_PID_PATH", st.PID_PATH)
    monkeypatch.setattr(mod, "_http", casino.sync_session())
    mod.state = mod.BotState()
    mod._apply_lane(lane)
    mod.init_db()
    s = mod.state
    s.access_token, s.lockdown_token, s.currency, s.game = "t", "l", "usdt", "limbo"
    s.dice_target, s.dice_condition = 50.5, "above"
    s.multiplier_target, s.strategy, s.strategy_key = 2.0, "Flat Bet", "1"
    s.base_bet = s.current_bet = st.MIN_BET
    s.db_write_mode = "sync"
    s.lanes_count, s.split_lanes, s.lane_split = 2, 2, "budget"
    for k, v in kw.items():
        setattr(s, k, v)
    s.start_balance = s.current_balance = casino.balance
    mod._init_lane_budget()
    s.session_id = mod._db_start_session()
    s.session_start = time.time()
    s.running = True
    return s


def test_two_lanes_in_threads_share_account(monkeypatch):
    account = {"balance": 10.0, "lock": threading.Lock()}
    c1 = _LaneCasino(account, ["win", "loss", "loss", "win", "loss"])
    c2 = _LaneCasino(account, ["loss", "loss", "win", "win"])
    m1 = _load_lane_module("stake_lane_a", monkeypatch)
    m2 = _load_lane_module("stake_lane_b", monkeypatch)
    try:
        s1 = _setup_lane(m1, 1, c1, monkeypatch, max_bets=5)
        # lane 2: Martingale 1.0 x2 on a 5.0 share; after two losses equity
        # is 2.0 and the next bet 4.0 → budget stop; recurring must not restart.
        s2 = _setup_lane(m2, 2, c2, monkeypatch, strategy="Martingale", strategy_key="2",
                         base_bet=1.0, current_bet=1.0, loss_mult=2.0, recurring=True,
                         recurring_delay=0)
        assert s1.lane_share == s2.lane_share == 5.0
        threads = [threading.Thread(target=m.betting_loop) for m in (m1, m2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads)
    finally:
        for m in (m1, m2):
            m.state.running = False
            m.logger.removeHandler(m._log_handler)

    assert s1.total_bets == 5 and s1.status == "STOPPED: Max bets reached: 5"
    assert [b["amount"] for b in c1.bets] == [st.MIN_BET] * 5      # flat, own chain
    assert [b["amount"] for b in c2.bets] == [1.0, 2.0]            # own Martingale chain
    assert s2.status.startswith("STOPPED: Lane budget exceeded (next bet 4.00000000")
    assert list(c2.script) == ["win", "win"]                       # stopped, no restart
    assert account["balance"] == pytest.approx(10.0 + c1.profit + c2.profit)

    with sqlite3.connect(st.DB_PATH) as c:
        rows = c.execute("SELECT lane, total_bets FROM sessions ORDER BY id").fetchall()
    assert rows == [(1, 5), (2, 2)]                                # one session per lane


# ── L6 benchmark (UserBalances at 1..M concurrent clients) ─────────────

def test_benchmark_lanes_probes_user_balances(saved_config, fake_casino, monkeypatch, capsys):
    monkeypatch.setattr(st.console, "width", 160)
    monkeypatch.setattr(st, "_bench_session", fake_casino.sync_session)
    bets = fake_casino.bet_posts
    monkeypatch.setattr(sys, "argv", ["stake.py", "--benchmark", "5", "--lanes", "3"])
    st.main()
    out = capsys.readouterr().out
    assert "Concurrent lanes" in out and "Recommended lanes:" in out
    # 1 + 2 + 3 clients x 5 read-only calls each, no bets
    assert fake_casino.gql_calls("UserBalances") == 30
    assert fake_casino.bet_posts == bets


def test_bench_lanes_counts_429_and_recommends_one(monkeypatch):
    class Limited:
        def post(self, *a, **k):
            return FakeResponse(429, {"errors": [{"message": "Too many requests"}]})
    monkeypatch.setattr(st, "_bench_session", Limited)
    r = st._bench_lanes(4, 2)
    assert (r["ok"], r["total"], r["s429"]) == (0, 8, 4 * 2)
    assert st.recommend_lanes(10.0, {2: r}) == 1


# ── L7 shared Cloudflare cookie cache ──────────────────────────────────

def test_cf_cache_save_is_atomic():
    st._cf_cookie_str, st._cf_user_agent = "cf_clearance=abc", "UA"
    try:
        st._cf_cache_save()
    finally:
        st._cf_cookie_str = st._cf_user_agent = ""
    path = Path(st.CF_CACHE_PATH)
    assert json.loads(path.read_text())["cookie_str"] == "cf_clearance=abc"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not list(path.parent.glob(path.name + "*.tmp"))
    assert st._cf_cache_load() and st._cf_cookie_str == "cf_clearance=abc"
    st._cf_cookie_str = st._cf_user_agent = ""


class _CfSession:
    """GraphQL AuthCheck is behind Cloudflare until the request carries the
    cf_clearance cookie. Records the thread of each blocked call."""

    def __init__(self, blocked_threads):
        self.blocked = blocked_threads

    def post(self, url, headers=None, json=None, timeout=None, **_):
        assert url.endswith(GQL) and json["operationName"] == "AuthCheck"
        if "cf_clearance=ok" in (headers or {}).get("Cookie", ""):
            return FakeResponse(200, {"data": {"user": {"id": "u1", "__typename": "User"}}})
        self.blocked.add(threading.get_ident())
        return FakeResponse(403, CF_PAGE)


def test_two_lanes_hit_cf_403_together_one_reauth(cli_state, monkeypatch):
    cli_state()
    blocked, solves = set(), []
    monkeypatch.setattr(st, "_http", _CfSession(blocked))
    monkeypatch.setattr(st, "_flaresolverr_available", lambda: True)
    deadline = time.monotonic() + 5

    def solve(site):
        # Hold the re-auth lock until the other lane is blocked by Cloudflare
        # too, so it must wait for the lock instead of solving on its own.
        while len(blocked) < 2 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        solves.append(site)
        st._cf_cookie_str, st._cf_user_agent = "cf_clearance=ok", "UA"
        st._cf_cache_save()
        return True
    monkeypatch.setattr(st, "_solve_cloudflare", solve)
    # each "lane" starts without in-memory cookies; only the shared cache file links them
    st._cf_cookie_str = st._cf_user_agent = ""

    results = []
    threads = [threading.Thread(target=lambda: results.append(st.api_test_connection()))
               for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        cache_cookie = json.loads(Path(st.CF_CACHE_PATH).read_text())["cookie_str"]
        st._cf_cookie_str = st._cf_user_agent = ""
    assert results == [True, True]
    assert len(blocked) == 2
    assert solves == ["https://stake.bet"]              # one re-auth for both lanes
    assert cache_cookie == "cf_clearance=ok"           # cache stays valid JSON
    assert Path(st.CF_CACHE_PATH + ".lock").exists()


def test_direct_connection_takes_no_cf_lock(cli_state):
    cli_state()
    assert st.api_test_connection()
    assert not Path(st.CF_CACHE_PATH + ".lock").exists()
