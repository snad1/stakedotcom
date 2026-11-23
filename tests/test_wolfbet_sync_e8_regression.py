"""stakedotcom cross-feature regression matrix: the real CLI and TG loops on the fake casino.

Each block drives one end-to-end scenario through the code paths the earlier
sync stories added (cycles, streak reset, error handling, live reload, stats,
export / backtest, pause, lanes, run-wide increment, exit reporting) and checks
the result against numbers recomputed independently from the scripted
outcomes. A behaviour stake does not have is a strict xfail naming the gap;
a scenario the casino API cannot support is a skip naming the reason.

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q
"""

import asyncio
import json
import random
import socket
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import stake as st
from shared.core.run_increment import load_run, save_run
from shared.core.stats import stats_date_window

from .test_wolfbet_sync_e5_lanes import _LaneCasino, _load_lane_module, _setup_lane
from .test_wolfbet_sync_e5_lanes import (
    test_two_lanes_hit_cf_403_together_one_reauth as _two_lanes_cf_403_one_reauth)
from .test_wolfbet_sync_e7 import _config as _ctl_config, _ctl, env  # noqa: F401  (fixture)

BET = st.MIN_BET
GAMES = ("limbo", "dice")


# ── helpers ──────────────────────────────────────────────
def _mixed_script(n, seed=7, max_losses=5):
    """n outcomes with at most `max_losses` losses in a row (Martingale stays affordable)."""
    rng, out, run = random.Random(seed), [], 0
    for _ in range(n):
        o = "loss" if rng.random() < 0.5 and run < max_losses else "win"
        run = run + 1 if o == "loss" else 0
        out.append(o)
    return out


def _martingale_expect(script, base, mult=2.0):
    """Bet sizes, profit, W/L and max streaks for Martingale x2 at 2x payout."""
    bet, bets, profit, w, l, streak, best_w, best_l = base, [], 0.0, 0, 0, 0, 0, 0
    for o in script:
        bets.append(bet)
        if o == "win":
            profit += bet * (mult - 1)
            w += 1
            streak = streak + 1 if streak > 0 else 1
            bet = base
        else:
            profit -= bet
            l += 1
            streak = streak - 1 if streak < 0 else -1
            bet = max(base, bet * 2)
        best_w, best_l = max(best_w, streak), max(best_l, -streak)
    return {"bets": bets, "profit": profit, "wins": w, "losses": l,
            "max_win": best_w, "max_loss": best_l}


def _rows(db, sql, args=()):
    with sqlite3.connect(db) as c:
        return c.execute(sql, args).fetchall()


def _sessions(db):
    return _rows(db, "SELECT id, started_at, ended_at, total_bets, profit FROM sessions ORDER BY id")


# ── 1. 500-bet Martingale, recomputed ────────────────────
@pytest.mark.parametrize("game", GAMES)
def test_cli_martingale_500_matches_recomputation(game, fake_casino, cli_state, run_cli):
    script = _mixed_script(500)
    exp = _martingale_expect(script, BET)
    fake_casino.queue(*script)
    s = cli_state(game=game, max_bets=500, strategy="Martingale", strategy_key="2",
                  loss_mult=2.0, db_write_mode="sync")
    start = s.start_balance

    run_cli()

    assert s.total_bets == 500 and (s.wins, s.losses) == (exp["wins"], exp["losses"])
    assert s.profit == pytest.approx(exp["profit"])
    assert s.current_balance == pytest.approx(start + exp["profit"])
    assert (s.max_win_streak, s.max_loss_streak) == (exp["max_win"], exp["max_loss"])
    assert [b["amount"] for b in fake_casino.bets] == pytest.approx(exp["bets"])
    rows = _rows(st.DB_PATH, "SELECT amount, state, profit FROM bets WHERE session_id=? ORDER BY id",
                 (s.session_id,))
    assert len(rows) == 500
    assert [r[1] for r in rows] == script
    assert sum(r[2] for r in rows) == pytest.approx(exp["profit"])


@pytest.mark.parametrize("game", GAMES)
def test_tg_martingale_500_matches_recomputation(game, fake_casino, tg_engine_factory, run_tg):
    script = _mixed_script(500, seed=11)
    exp = _martingale_expect(script, BET)
    fake_casino.queue(*script)
    e = tg_engine_factory(game=game, max_bets=500, strategy="Martingale", strategy_key="2",
                          loss_mult=2.0)
    run_tg(e)

    assert e.total_bets == 500 and (e.wins, e.losses) == (exp["wins"], exp["losses"])
    assert e.profit == pytest.approx(exp["profit"])
    assert (e.max_win_streak, e.max_loss_streak) == (exp["max_win"], exp["max_loss"])
    assert [b["amount"] for b in fake_casino.bets] == pytest.approx(exp["bets"])
    rows = _rows(e.db_path, "SELECT state, profit FROM bets WHERE session_id=? ORDER BY id",
                 (e.session_id,))
    assert [r[0] for r in rows] == script
    assert sum(r[1] for r in rows) == pytest.approx(exp["profit"])


# ── 2. streak reset ──────────────────────────────────────
SR = ("win", "win", "loss", "loss", "loss", "win")      # profit 0 at the breaking win


def test_cli_streak_reset_restarts_with_srdelay_keeps_base_rearms(fake_casino, cli_state,
                                                                  run_cli, sleeps):
    pi = BET / 10
    # bump after two wins; the third win keeps the breaking win in profit
    fake_casino.queue("win", "win", "win", "loss", "loss", "loss", "win", "win")
    s = cli_state(max_bets=None, streak_reset_losses=3, streak_reset_delay=600,
                  recurring=False, recurring_delay=30, db_write_mode="sync",
                  profit_increment=pi, profit_threshold=2 * BET,
                  next_profit_milestone=2 * BET)
    run_cli()

    rows = _sessions(st.DB_PATH)
    assert len(rows) >= 2 and rows[0][3] == 7 and rows[0][4] > 0
    assert sum(sleeps) == pytest.approx(600)           # srdelay, not recurring_delay
    assert s.base_bet == pytest.approx(BET + pi)       # the bump is kept into the new row
    assert s.next_profit_milestone == pytest.approx(2 * BET)   # re-armed for the new cycle


def test_cli_streak_reset_never_in_the_red(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss", "loss", "win")
    s = cli_state(max_bets=4, streak_reset_losses=3)
    run_cli()
    assert s.status == "STOPPED: Max bets reached: 4"
    assert len(_sessions(st.DB_PATH)) == 1


def test_tg_streak_reset_stops_with_the_reset_reason(fake_casino, tg_engine_factory, run_tg):
    fake_casino.queue(*SR)
    e = tg_engine_factory(max_bets=None, streak_reset_losses=3)
    run_tg(e)
    assert e.stops and "Loss streak reset: 3 losses then win" in e.stops[-1]


# ── 3. recurring cycles ──────────────────────────────────
def test_cli_recurring_three_cycles_three_rows_one_run(fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue(*["win"] * 6)
    s = cli_state(max_bets=2, recurring=True, recurring_delay=45, db_write_mode="sync")
    run_started = s.run_started_at

    run_cli()

    rows = _sessions(st.DB_PATH)
    assert [r[3] for r in rows[:3]] == [2, 2, 2] and all(r[2] for r in rows)
    assert sum(sleeps) == pytest.approx(3 * 45)        # a pause before cycles 2, 3 and 4
    assert s.run_started_at == run_started              # one run clock across cycles
    bet_time = sum((st.datetime.fromisoformat(r[2]) - st.datetime.fromisoformat(r[1])).total_seconds()
                   for r in rows)
    span = (st.datetime.fromisoformat(rows[-1][2]) - st.datetime.fromisoformat(rows[0][1])).total_seconds()
    assert span >= bet_time


# ── 4. insufficient balance mid-chain ────────────────────
def test_cli_insufficient_mid_chain_stops_saved_no_restart(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss", "insufficient_balance", "win")
    s = cli_state(max_bets=None, strategy="Martingale", strategy_key="2", loss_mult=2.0,
                  recurring=True, recurring_delay=0, db_write_mode="sync")
    run_cli()
    rows = _sessions(st.DB_PATH)
    assert s.status == "STOPPED: Insufficient balance"
    assert len(rows) == 1 and rows[0][2] and rows[0][3] == 2
    assert list(fake_casino.script) == ["win"]


def test_tg_insufficient_mid_chain_stops(fake_casino, tg_engine_factory, run_tg):
    fake_casino.queue("loss", "loss", "insufficient_balance", "win")
    e = tg_engine_factory(max_bets=None, strategy="Martingale", strategy_key="2", loss_mult=2.0)
    run_tg(e)
    assert e.total_bets == 2 and list(fake_casino.script) == ["win"]
    assert e.stops and "nsufficient" in e.stops[-1]


# ── 5. 429 and timeouts ──────────────────────────────────
def test_cli_429_backs_off_then_continues_same_session(fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("http_429", "http_429", "http_429", "win", "win")
    s = cli_state(max_bets=2)
    sid = s.session_id
    run_cli()
    assert s.total_bets == 2 and s.session_id == sid
    assert [d for d in sleeps if d >= 1][:3] == [2.0, 4.0, 8.0]


def test_cli_timeouts_retry_and_continue(fake_casino, cli_state, run_cli):
    fake_casino.queue("timeout", "timeout", "timeout", "win")
    s = cli_state(max_bets=1)
    sid = s.session_id
    run_cli()
    assert s.total_bets == 1 and s.session_id == sid


def test_tg_429_then_ok(fake_casino, tg_engine_factory, run_tg):
    fake_casino.queue("http_429", "http_429", "http_429", "win", "win")
    e = tg_engine_factory(max_bets=2)
    run_tg(e)
    assert e.total_bets == 2


def test_tg_three_timeouts_recreate_the_client(fake_casino, tg_engine_factory, run_tg, monkeypatch):
    fake_casino.queue("timeout", "timeout", "timeout", "win")
    e = tg_engine_factory(max_bets=1)
    calls = []
    real = type(e)._recreate_http

    async def counting(self):
        calls.append(1)
        await real(self)
    monkeypatch.setattr(type(e), "_recreate_http", counting)
    run_tg(e)
    assert e.total_bets == 1
    assert len(calls) >= 2                                # start + after three timeouts


# ── 6. Cloudflare ────────────────────────────────────────
@pytest.mark.parametrize("game", GAMES)
def test_tg_cf_403_mid_session_reauths_and_continues(game, fake_casino, tg_engine_factory, run_tg):
    fake_casino.queue("win", "win", "cf_403", "loss", "win")
    e = run_tg(tg_engine_factory(game, max_bets=4))
    assert (e.total_bets, e.wins, e.losses) == (4, 3, 1)
    assert fake_casino.gql_calls("AuthCheck") == 2           # start + one _cf_reauth
    assert e.current_balance == pytest.approx(fake_casino.balance)


def test_cli_cf_403_mid_session_continues(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "cf_403", "loss", "win")
    s = cli_state(max_bets=3)
    sid = s.session_id
    run_cli()
    assert (s.total_bets, s.session_id) == (3, sid)


@pytest.mark.xfail(strict=True, reason="gap: the CLI treats a mid-session Cloudflare 403 as a plain "
                                       "error and backs off; only the TG engine calls _cf_reauth")
def test_cli_cf_403_mid_session_reauths(fake_casino, cli_state, run_cli, monkeypatch):
    calls = []
    monkeypatch.setattr(st, "_cf_reauth", lambda *a, **k: calls.append(1) or (None, "x"))
    fake_casino.queue("win", "cf_403", "win")
    cli_state(max_bets=2)
    run_cli()
    assert calls


def test_lanes_concurrent_cf_403_one_reauth_cookie_file_json(cli_state, monkeypatch):
    _two_lanes_cf_403_one_reauth(cli_state, monkeypatch)


# ── 7. live reload + config-set ──────────────────────────
def test_cli_sighup_applies_live_keys_only_and_config_set_round_trips(cli_state):
    s = cli_state(max_loss=1.0, game="limbo")
    s.running = False
    st.save_config()
    data = json.loads(Path(st.CONFIG_PATH).read_text())
    data.update(max_loss=3.0, bet_delay=0.25, game="dice")
    Path(st.CONFIG_PATH).write_text(json.dumps(data))

    applied = st._reload_live_config()

    assert {"max_loss", "bet_delay"} <= set(applied) and "game" not in applied
    assert (s.max_loss, s.bet_delay, s.game) == (3.0, 0.25, "limbo")
    st.cmd_config_set(["maxloss=7", "delay=0.1"])
    saved = json.loads(Path(st.CONFIG_PATH).read_text())
    assert (saved["max_loss"], saved["bet_delay"]) == (7.0, 0.1)


def test_tg_mutate_changes_live_keys(tg_engine_factory):
    e = tg_engine_factory(max_loss=1.0)
    msg = asyncio.run(e.mutate({"max_loss": "2.5", "bet_delay": "0.2"}))
    assert e.max_loss == 2.5 and e.bet_delay == 0.2 and msg


# ── 8. stats / analytics ─────────────────────────────────
STATS = [
    (1, "2026-11-13T10:00:00", "2026-11-13T11:00:00", "limbo", 10, 4, 6, -0.002, 0.01, 1),
    (2, "2026-11-13T11:00:00", "2026-11-13T11:30:00", "dice", 20, 12, 8, 0.004, 0.02, 1),
    (3, "2026-11-13T14:30:00", "2026-11-13T15:00:00", "limbo", 30, 15, 15, 0.001, 0.03, 2),
]


@pytest.fixture
def stats_db():
    st.init_db()
    with sqlite3.connect(st.DB_PATH) as c:
        c.executemany(
            "INSERT INTO sessions (id, started_at, ended_at, currency, game, strategy, base_bet, "
            "multiplier, total_bets, wins, losses, profit, wagered, lane) "
            "VALUES (?,?,?,'usdt',?,'Flat',0.001,2.0,?,?,?,?,?,?)", STATS)
    return st.DB_PATH


def test_cli_stats_totals_equal_hand_computed(stats_db, capsys):
    where, params, _ = stats_date_window()
    t = st._analytics_lifetime(where, params)
    assert (t["sessions_total"], t["bets_total"], t["wins_total"], t["losses_total"]) == (3, 60, 31, 29)
    assert t["profit_total"] == pytest.approx(0.003)
    where, params, _ = stats_date_window("2026-11-13 11:00", "2026-11-13 14:00")
    t = st._analytics_lifetime(where, params)
    assert (t["sessions_total"], t["bets_total"]) == (1, 20)
    assert t["profit_total"] == pytest.approx(0.004)
    st.cmd_stats(since="2026-11-13 14:00")
    out = capsys.readouterr().out
    assert "Session #3" in out and "Session #1" not in out


# ── 9. export + backtest ─────────────────────────────────
def test_cli_export_round_trip_and_backtest_repeatable(fake_casino, cli_state, run_cli,
                                                       tmp_path, monkeypatch):
    fake_casino.queue("win", "loss", "win")
    s = cli_state(max_bets=3, db_write_mode="sync")
    run_cli()
    st._db_save_session()
    path = tmp_path / "s.json"
    assert st._export_session(s.session_id, str(path)) == (3, "json")
    data = json.loads(path.read_text())
    assert data["session"]["total_bets"] == 3
    assert [b["state"] for b in data["bets"]] == ["win", "loss", "win"]

    def no_net(*a, **k):
        raise AssertionError("backtest touched the network")
    monkeypatch.setattr(socket.socket, "connect", no_net)
    args = SimpleNamespace(preset=None, replay_session=None, simulate=300, seed=9)
    st.state = st.BotState()
    st._run_backtest(args)
    first = (st.state.total_bets, round(st.state.profit, 8))
    st.state = st.BotState()
    st._run_backtest(args)
    assert (st.state.total_bets, round(st.state.profit, 8)) == first


# ── 10. pause / resume freezes uptime ────────────────────
@pytest.mark.xfail(strict=True, reason="gap: a user pause (SIGUSR1 / p) only sets `paused`; uptime "
                                       "keeps counting (WolfBet CLI behaves the same; only the "
                                       "recurring pause freezes it)")
def test_cli_user_pause_freezes_uptime(cli_state, monkeypatch):
    s = cli_state()
    s.session_start = 1000.0
    monkeypatch.setattr(st.time, "time", lambda: 1100.0)
    s.paused = True                                     # what SIGUSR1 and `p` do
    st._save_state_file()
    before = json.loads(Path(st.STATE_PATH).read_text())["uptime_sec"]
    monkeypatch.setattr(st.time, "time", lambda: 1200.0)
    st._save_state_file()
    assert json.loads(Path(st.STATE_PATH).read_text())["uptime_sec"] == before


def test_tg_pause_freezes_uptime_and_resume_continues(tg_engine_factory, monkeypatch):
    e = tg_engine_factory()
    e.session_start = 1000.0
    monkeypatch.setattr("tg.engine.time.time", lambda: 1100.0)
    e.pause()
    up = e.get_status()["uptime_sec"]
    monkeypatch.setattr("tg.engine.time.time", lambda: 1300.0)
    e._status_cache, e._status_cache_ts = None, 0.0
    assert e.get_status()["uptime_sec"] == up == 100
    e.resume()
    assert not e.paused


# ── 11. lanes (CLI) ──────────────────────────────────────
@pytest.fixture
def lane_paths(monkeypatch):
    monkeypatch.setattr(st, "_BASE_STATE_PATH", st.STATE_PATH)
    monkeypatch.setattr(st, "_BASE_PID_PATH", st.PID_PATH)
    fmt = st._log_handler.formatter
    yield
    st._log_handler.setFormatter(fmt)


def _run_two_lanes(monkeypatch, s1_kw, s2_kw, script1, script2, split):
    account = {"balance": 10.0, "lock": threading.Lock()}
    c1, c2 = _LaneCasino(account, script1), _LaneCasino(account, script2)
    m1 = _load_lane_module("stake_e8_lane_a", monkeypatch)
    m2 = _load_lane_module("stake_e8_lane_b", monkeypatch)
    try:
        s1 = _setup_lane(m1, 1, c1, monkeypatch, lane_split=split, **s1_kw)
        s2 = _setup_lane(m2, 2, c2, monkeypatch, lane_split=split, **s2_kw)
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
    return s1, s2, c1, c2


def test_lanes_split_none_full_bets_no_guard_and_lane_stats(lane_paths, monkeypatch):
    s1, s2, c1, c2 = _run_two_lanes(
        monkeypatch, {"max_bets": 4}, {"max_bets": 3},
        ["win", "loss", "win", "win"], ["loss", "win", "loss"], "none")
    assert s1.lane_share is None and s2.lane_share is None
    assert [b["amount"] for b in c1.bets + c2.bets] == [BET] * 7
    rows = _rows(st.DB_PATH, "SELECT lane, total_bets FROM sessions ORDER BY id")
    assert sorted(rows) == [(1, 4), (2, 3)]
    where, params, _ = stats_date_window(lane=2)
    t = st._analytics_lifetime(where, params)
    assert t["bets_total"] == 3 and t["profit_total"] == pytest.approx(c2.profit)


def test_lanes_split_budget_stops_lane_two_only(lane_paths, monkeypatch):
    s1, s2, c1, c2 = _run_two_lanes(
        monkeypatch, {"max_bets": 5},
        {"strategy": "Martingale", "strategy_key": "2", "base_bet": 1.0, "current_bet": 1.0,
         "loss_mult": 2.0, "recurring": True, "recurring_delay": 0},
        ["win", "loss", "loss", "win", "loss"], ["loss", "loss", "win", "win"], "budget")
    assert s1.status == "STOPPED: Max bets reached: 5"
    assert s2.status.startswith("STOPPED: Lane budget exceeded")
    assert list(c2.script) == ["win", "win"]


def test_lanes_split_bet_halves_base_saves_full_and_override_reloads(cli_state, lane_paths):
    cli_state(base_bet=0.01)
    st.state.running = False
    st.save_config()
    data = json.loads(Path(st.CONFIG_PATH).read_text())
    data["lanes"] = {"count": 2, "split": "bet"}
    Path(st.CONFIG_PATH).write_text(json.dumps(data))
    st.state = st.BotState()
    st._apply_lane(2)
    assert st.load_config()
    assert st.state.base_bet == pytest.approx(0.005)
    st.save_config()                                   # lane 2 never writes the shared file
    assert json.loads(Path(st.CONFIG_PATH).read_text())["base_bet"] == pytest.approx(0.01)
    data = json.loads(Path(st.CONFIG_PATH).read_text())
    data["lanes"]["overrides"] = {"2": {"max_loss": 0.5}}
    Path(st.CONFIG_PATH).write_text(json.dumps(data))
    applied = st._reload_live_config()
    assert applied.get("max_loss", (None, None))[1] == 0.5 and st.state.max_loss == 0.5


# ── 12. run-wide increment ───────────────────────────────
PI, PT = BET / 10, 3 * BET


def test_cli_run_scope_across_cycles_restart_stale_and_off(fake_casino, cli_state, run_cli):
    fake_casino.queue(*["win"] * 6)
    s = cli_state(max_bets=2, recurring=True, recurring_delay=0, db_write_mode="sync",
                  profit_increment=PI, profit_threshold=PT, profit_increment_scope="run")
    s.next_profit_milestone = PT
    st._init_run()
    st._attach_run(s.session_id)
    run_cli()

    assert all(r[4] < PT for r in _sessions(st.DB_PATH))                # no session reaches PT
    run = load_run(st._run_path())
    assert run["bumps"] >= 1                                            # the run total did
    assert s.next_profit_milestone > PT                                 # milestone kept
    base_after = s.base_bet

    s.base_bet = s.base_bet_disk = BET                                  # restart: config base
    st._init_run()
    assert s.run_bumps == run["bumps"] and s.base_bet == pytest.approx(base_after)

    Path(st.DB_PATH).unlink()                                           # reset archives the DB
    st.init_db()
    save_run(st._run_path(), {"first_session": 4, "started_at": "t", "bumps": 2})
    s.base_bet = BET
    st._init_run()
    assert s.run_first_session is None and s.run_bumps == 0             # stale: new run

    save_run(st._run_path(), {"first_session": 1, "started_at": "t", "bumps": 2})
    s.profit_increment = None
    st._init_run()
    assert not Path(st._run_path()).exists()                            # pi=none removes it


def test_cli_session_scope_unchanged(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "win", "win")
    s = cli_state(max_bets=3, profit_increment=BET, profit_threshold=2 * BET,
                  next_profit_milestone=2 * BET)
    run_cli()
    # bumps at 2*BET and again at 4*BET (third win paid at the bumped base)
    assert s.base_bet == pytest.approx(3 * BET) and not Path(st._run_path()).exists()


def test_cli_run_files_per_lane_sum_own_sessions(cli_state, monkeypatch):
    s = cli_state(profit_increment=PI, profit_threshold=PT, profit_increment_scope="run")
    with sqlite3.connect(st.DB_PATH) as c:
        c.execute("UPDATE sessions SET profit=0.002, lane=1 WHERE id=?", (s.session_id,))
        c.execute("INSERT INTO sessions (started_at, currency, game, strategy, base_bet, "
                  "multiplier, start_balance, lane, profit) VALUES ('t','usdt','limbo','F',"
                  "0.001,2,10,2,0.5)")
    assert st._run_path(1) != st._run_path(2)
    with sqlite3.connect(st.DB_PATH) as c:
        c.execute("INSERT INTO sessions (started_at, currency, game, strategy, base_bet, "
                  "multiplier, start_balance, lane, profit) VALUES ('t','usdt','limbo','F',"
                  "0.001,2,10,1,0.003)")
    s.lane = 1
    # stake leaves the live session out of the finished-session sum
    assert st._run_profit_done_from_db(s.session_id) == pytest.approx(0.003)


def test_tg_run_scope_second_session_starts_bumped(fake_casino, tg_engine_factory, run_tg):
    fake_casino.queue(*["win"] * 4)
    e1 = tg_engine_factory(max_bets=2, profit_increment=PI, profit_threshold=3 * BET,
                           profit_increment_scope="run")
    run_tg(e1)
    e2 = tg_engine_factory(max_bets=2, profit_increment=PI, profit_threshold=3 * BET,
                           profit_increment_scope="run")
    run_tg(e2)
    assert e2.run_bumps >= 1
    assert e2.base_bet == pytest.approx(BET + e2.run_bumps * PI)


# ── 13. increment on a loss + exit reporting ─────────────
def test_cli_bump_on_a_loss_keeps_betting_martingale(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss", "win")
    s = cli_state(max_bets=3, strategy="Martingale", strategy_key="2", loss_mult=2.0,
                  db_write_mode="sync", profit_increment=PI, profit_threshold=PT,
                  profit_increment_scope="run")
    s.run_first_session, s.run_profit_done = s.session_id, 10 * PT
    s.next_profit_milestone = PT                        # bumps pending: one lands on each loss
    run_cli()

    amounts = [b["amount"] for b in fake_casino.bets]
    assert len(amounts) == 3 and s.status == "STOPPED: Max bets reached: 3"
    assert amounts[1] == pytest.approx(max(BET + PI, 2 * BET))
    assert amounts[2] == pytest.approx(max(BET + 2 * PI, 4 * BET))
    assert s.run_bumps >= 2
    rows = _sessions(st.DB_PATH)
    assert len(rows) == 1 and rows[0][3] == 3
    assert len(_rows(st.DB_PATH, "SELECT id FROM bets")) == 3


def test_exit_reason_summary_install_enable_and_doctor(env, capsys):
    st.state.status = "STOPPED: Max loss hit: -0.00500000"
    st._print_summary()
    assert "Stop reason" in capsys.readouterr().out
    src = Path(st.__file__).read_text()
    assert "DAEMON EXIT  session=%d  bets=%d  profit=%+.8f  reason=%s" in src
    install = (Path(st.__file__).parent / "install.sh").read_text()
    assert "systemctl --user enable ${SERVICE_NAME} " in install
    _ctl_config(env)
    assert "[WARN]  stake.service is not enabled" in _ctl(env, "doctor").stdout
