"""E3-S3 — stakedotcom CLI betting behaviour (WolfBet v2.16.0 .. v2.32.1).

Recurring / streak-reset cycles (new sessions row per cycle), streak delays,
streakbet_loss, basebet_pct, milestones, frozen uptime, terminal insufficient
balance, config persistence and SIGHUP live reload. Modelled on WolfBet
test_phase2_helpers, 5f, 5o, 5p, 5q, 5u, 6b, 9 (CLI half) and 9b (terminal).

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q
"""

import inspect
import json
import logging
import re
import sqlite3
import time
from datetime import datetime

import pytest

import stake as st

GAMES = ["limbo", "dice"]
B = st.MIN_BET


def _strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _sessions():
    conn = sqlite3.connect(st.DB_PATH)
    try:
        return conn.execute(
            "SELECT id, ended_at, total_bets, start_balance, profit FROM sessions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _bet_rows():
    conn = sqlite3.connect(st.DB_PATH)
    try:
        return conn.execute("SELECT session_id, amount, state FROM bets ORDER BY rowid").fetchall()
    finally:
        conn.close()


# ── recurring cycles ──────────────────────────────────────
@pytest.mark.parametrize("game", GAMES)
def test_recurring_opens_second_session_after_delay(game, fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("win", "win", "win", "win")
    cli_state(game, max_bets=2, recurring=True, recurring_delay=60.0)
    s = run_cli()

    rows = _sessions()
    # cycle 1 + cycle 2 closed, cycle 3 ended on the empty script (insufficient)
    assert len(rows) == 3
    assert all(r[1] for r in rows)
    assert [r[2] for r in rows] == [2, 2, 0]
    assert sum(sleeps) == pytest.approx(120.0)
    assert s.status == "STOPPED: Insufficient balance"
    assert {r[0] for r in _bet_rows()} == {rows[0][0], rows[1][0]}


def test_recurring_off_single_session(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "win")
    s = cli_state(max_bets=2)
    run_cli()
    assert len(_sessions()) == 1
    assert s.status == "STOPPED: Max bets reached: 2"


def test_recurring_no_restart_on_max_loss(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss")
    s = cli_state(max_bets=None, max_loss=2 * B * 0.99, recurring=True)
    run_cli()
    assert len(_sessions()) == 1
    assert s.status.startswith("STOPPED: Max loss hit")


def test_recurring_no_restart_when_profit_negative(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss")
    s = cli_state(max_bets=2, recurring=True)
    run_cli()
    assert len(_sessions()) == 1
    assert s.status == "STOPPED: Max bets reached: 2"


def test_quit_during_pause_opens_no_new_session(fake_casino, cli_state, run_cli, monkeypatch):
    fake_casino.queue("win", "win")
    cli_state(max_bets=2, recurring=True, recurring_delay=60.0)
    slept = []

    def quit_on_sleep(d):
        slept.append(d)
        st.state.running = False          # [Q] pressed during the pause
    monkeypatch.setattr(st.time, "sleep", quit_on_sleep)
    run_cli()

    assert slept == [1.0]
    assert len(_sessions()) == 1


def test_balance_refreshed_at_cycle_start(fake_casino, cli_state, run_cli, monkeypatch):
    fake_casino.queue("win", "win")
    cli_state(max_bets=1, recurring=True, recurring_delay=1.0)
    gql_before = fake_casino.gql_calls("UserBalances")

    def deposit_during_pause(d):
        fake_casino.balance = 25.0       # real balance moved outside this bot
    monkeypatch.setattr(st.time, "sleep", deposit_during_pause)
    run_cli()

    rows = _sessions()
    assert rows[1][3] == pytest.approx(25.0)          # cycle 2 start_balance
    assert fake_casino.gql_calls("UserBalances") - gql_before == 2
    assert st.state.current_balance == pytest.approx(fake_casino.balance)


def test_balance_refresh_failure_keeps_local(cli_state, monkeypatch):
    s = cli_state()
    s.current_balance = 7.5
    monkeypatch.setattr(st, "api_get_balances", lambda: [])
    st._refresh_balance()
    assert s.current_balance == 7.5


def test_reset_rearms_and_keeps_bumped_base(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "win")
    s = cli_state(max_bets=1, recurring=True, recurring_delay=0.0,
                  profit_threshold=B, profit_increment=B, next_profit_milestone=B)
    run_cli()

    assert [b["amount"] for b in fake_casino.bets] == pytest.approx([B, 2 * B])
    assert s.base_bet == pytest.approx(3 * B)
    # cycle 3 re-armed the milestone to the threshold before its first bet
    assert s.next_profit_milestone == pytest.approx(B)


def test_reset_zeroes_counters(fake_casino, cli_state):
    s = cli_state()
    s.total_bets, s.wins, s.losses, s.profit = 5, 3, 2, 0.5
    s.current_streak, s.last_broken_loss_streak = 2, 4
    s.recent_bets.append({"n": 1})
    old_sid = s.session_id
    assert st._reset_session_for_recurring(0.0)
    assert (s.total_bets, s.wins, s.losses, s.profit) == (0, 0, 0, 0.0)
    assert (s.current_streak, s.last_broken_loss_streak) == (0, 0)
    assert not s.recent_bets
    assert s.session_id != old_sid
    assert s.session_bet_ended_at is None and s.session_pause_started_at is None


# ── streak reset ──────────────────────────────────────────
@pytest.mark.parametrize("game", GAMES)
def test_streak_reset_fires_and_restarts(game, fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("loss", "loss", "loss", "win")
    cli_state(game, max_bets=None, strategy_key="2", loss_mult=2.0,
              streak_reset_losses=3, streak_reset_delay=7.0, recurring_delay=60.0)
    s = run_cli()

    rows = _sessions()
    assert len(rows) == 2
    assert rows[0][2] == 4
    assert sum(sleeps) == pytest.approx(7.0)       # its own delay, not recurring_delay
    assert s.status == "STOPPED: Insufficient balance"


def test_streak_reset_reason_text(cli_state):
    s = cli_state()
    s.streak_reset_losses, s.current_streak, s.last_broken_loss_streak = 3, 1, 22
    assert st.should_stop() == (True, "Loss streak reset: 22 losses then win")


@pytest.mark.parametrize("n,streak,broken,profit", [
    (3, 1, 3, -0.1),    # in the red
    (3, 1, 2, 0.1),     # below N
    (None, 1, 5, 0.1),  # feature off
    (3, 2, 3, 0.1),     # second win
])
def test_streak_reset_no_fire(cli_state, n, streak, broken, profit):
    s = cli_state(max_bets=None)
    s.streak_reset_losses, s.current_streak = n, streak
    s.last_broken_loss_streak, s.profit = broken, profit
    assert st.should_stop() == (False, "")


def test_streak_reset_in_red_does_not_stop(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss", "loss", "win")
    s = cli_state(max_bets=4, streak_reset_losses=3)     # flat: -2 units after the win
    run_cli()
    assert len(_sessions()) == 1
    assert s.status == "STOPPED: Max bets reached: 4"


def test_outcome_records_last_broken(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss", "win")
    s = cli_state(max_bets=3)
    run_cli()
    assert s.last_broken_loss_streak == 2 and s.current_streak == 1


# ── loop hooks ────────────────────────────────────────────
def test_streak_delay_sleeps(fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("loss", "loss", "win")
    cli_state(max_bets=3, streak_delay_loss=(2, 1.5))
    run_cli()
    assert sleeps.count(1.5) == 1


def test_streakbet_loss_scales_next_bet(fake_casino, cli_state, run_cli):
    fake_casino.queue("loss", "loss", "win")
    cli_state(max_bets=3, streakbet_loss=(2, 3.0))
    run_cli()
    assert [b["amount"] for b in fake_casino.bets] == pytest.approx([B, B, 3 * B])


def test_basebet_pct(cli_state):
    s = cli_state()
    s.basebet_pct = 0.0
    assert not st._apply_basebet_pct()
    s.basebet_pct, s.current_balance = 0.001, 10.0
    assert st._apply_basebet_pct()
    assert s.base_bet == pytest.approx(0.01) and s.current_bet == pytest.approx(0.01)
    assert not st._apply_basebet_pct()


def test_basebet_pct_applied_every_10_bets(fake_casino, cli_state, run_cli):
    fake_casino.queue(*["win"] * 11)
    s = cli_state(max_bets=11, basebet_pct=0.001)
    run_cli()
    after_ten = fake_casino.bets[9]["balance"]
    assert fake_casino.bets[10]["amount"] == pytest.approx(round(after_ten * 0.001, 8))
    assert s.base_bet == pytest.approx(round(after_ten * 0.001, 8))


def test_check_milestone(cli_state):
    s = cli_state()
    s.milestone_bets, s.total_bets = 10, 10
    assert st._check_milestone("loss") == "10 bets"
    s.milestone_bets, s.milestone_wins, s.wins = 0, 5, 5
    assert st._check_milestone("win") == "5 wins"
    s.milestone_wins, s.milestone_losses, s.losses = 0, 3, 3
    assert st._check_milestone("loss") == "3 losses"
    s.milestone_losses, s.milestone_profit, s.profit = 0, 0.01, 0.025
    assert "profit" in st._check_milestone("win")
    assert st._check_milestone("win") is None


# ── insufficient balance terminal + bet error envelope ────
def test_insufficient_terminal_even_with_recurring(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "insufficient_balance")
    s = cli_state(max_bets=100, recurring=True, streak_reset_losses=1)
    run_cli()
    assert s.status == "STOPPED: Insufficient balance"
    assert s._insufficient_balance
    assert len(_sessions()) == 1


def test_http200_errors_is_not_a_bet(fake_casino, cli_state, run_cli, sleeps):
    fake_casino.queue("api_error_balance", "win")
    s = cli_state(max_bets=1)
    run_cli()
    assert not s._insufficient_balance
    assert s.total_bets == 1 and s.losses == 0
    assert _bet_rows() == [(s.session_id, pytest.approx(B), "win")]
    assert 2.0 in sleeps                     # took the error backoff


def test_http400_balance_message_not_insufficient(fake_casino, cli_state, run_cli):
    fake_casino.queue("http_error_balance", "win")
    s = cli_state(max_bets=1)
    run_cli()
    assert not s._insufficient_balance
    assert s.status == "STOPPED: Max bets reached: 1"


@pytest.mark.parametrize("parse,key", [(st._limbo_parse, "resultMultiplier"),
                                       (st._dice_parse, "resultTarget")])
def test_parsers_read_nested_state_result(parse, key):
    r = parse({"amount": 1, "payout": 2, "payoutMultiplier": 2, "state": {"result": 3.5}})
    assert r["result_value"] == 3.5
    r = parse({"amount": 1, "payout": 0, "payoutMultiplier": 0, key: 1.25,
               "state": {"result": 9}})
    assert r["result_value"] == 1.25


# ── uptime / state file / display ─────────────────────────
def test_fresh_state_not_frozen():
    s = st.BotState()
    assert s.session_bet_ended_at is None and s.session_pause_started_at is None


def test_uptime_freezes_during_pause(cli_state):
    s = cli_state()
    now = time.time()
    s.session_start = now - 420
    s.session_bet_ended_at = s.session_pause_started_at = now - 120
    s.session_pause_total = 3600.0
    s.run_started_at = now - 5000
    up = st._uptime_fields()
    assert up["uptime_sec"] == pytest.approx(300, abs=2)
    assert up["pause_remaining_sec"] == pytest.approx(3480, abs=2)
    assert up["run_uptime_sec"] == pytest.approx(5000, abs=2)
    s.session_pause_started_at = now - 4000
    assert st._uptime_fields()["pause_remaining_sec"] == 0


def test_run_uptime_spans_cycles(fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "win")
    s = cli_state(max_bets=1, recurring=True, recurring_delay=0.0)
    s.run_started_at = time.time() - 1000
    run_cli()
    up = st._uptime_fields()
    assert up["run_uptime_sec"] >= 1000 and up["uptime_sec"] < 1000


def test_db_ended_at_uses_frozen_bet_end(cli_state):
    s = cli_state()
    frozen = time.time() - 3600
    s.session_bet_ended_at = frozen
    st._db_save_session()
    ended = [r for r in _sessions() if r[0] == s.session_id][0][1]
    assert ended == datetime.fromtimestamp(frozen).isoformat()


def test_state_file_writes_cycle_fields(cli_state):
    s = cli_state(recurring=True, recurring_delay=45.0, streak_reset_losses=5,
                  profit_threshold=0.01, next_profit_milestone=0.02,
                  streak_delay_loss=(3, 0.5))
    st._save_state_file()
    with open(st.STATE_PATH) as f:
        d = json.load(f)
    assert d["recurring"] is True and d["recurring_delay_sec"] == 45
    assert d["streak_reset_losses"] == 5 and d["streak_reset_delay_sec"] == 600
    assert d["next_profit_milestone"] == 0.02
    assert d["streak_delay_loss"] == "3:0.5"
    for k in ("uptime_sec", "run_uptime_sec", "pause_remaining_sec", "pause_total_sec"):
        assert k in d


def _row(**kw):
    d = {"profit": 0.0, "total_bets": 0, "wins": 0, "current_balance": 1.0}
    d.update(kw)
    return _strip_ansi(st._build_config_progress_row(d))


def test_cfg_ms_per_window_progress():
    plain = _row(profit=0.025, profit_threshold=0.01, profit_increment=0.001,
                 next_profit_milestone=0.03)
    assert "Ms +0.00500000/0.01000000 (50%)" in plain


def test_cfg_ms_right_after_bump_is_zero():
    plain = _row(profit=0.02, profit_threshold=0.01, profit_increment=0.001,
                 next_profit_milestone=0.03)
    assert "(0%)" in plain


def test_cfg_ms_legacy_state_file_falls_back_to_threshold():
    plain = _row(profit=0.005, profit_threshold=0.01, profit_increment=0.001)
    assert "(50%)" in plain


def test_cfg_rec_and_sr_segments():
    plain = _row(recurring=True, recurring_delay_sec=60,
                 streak_reset_losses=5, streak_reset_delay_sec=600)
    assert "Rec on(60s)" in plain and "SR 5L→600s" in plain


def test_tui_cfg_feeds_next_milestone(cli_state):
    s = cli_state(profit_threshold=0.01, profit_increment=0.001,
                  next_profit_milestone=0.03, streak_reset_losses=4)
    s.profit = 0.025
    plain = _strip_ansi(st._build_config_progress_row(st._state_dict_from_botstate()))
    assert "(50%)" in plain and "SR 4L→600s" in plain


def test_pause_label_and_monitor_source():
    assert st._pause_label({"pause_remaining_sec": 30, "pause_total_sec": 60}) == \
        "◐ PAUSED 0:00:30/0:01:00"
    assert "pause_remaining_sec" in inspect.getsource(st._build_monitor_screen)
    assert "Recurring pause" in inspect.getsource(st.cmd_status)


# ── persistence ───────────────────────────────────────────
_NEW = {
    "recurring": True, "recurring_delay": 90.0,
    "streak_reset_losses": 7, "streak_reset_delay": 300.0,
    "streak_delay_loss": (3, 0.5), "streak_delay_win": (2, 1.0),
    "streak_delay_bets": (100, 2.0), "streakbet_loss": (4, 1.5),
    "basebet_pct": 0.002, "milestone_bets": 50, "milestone_wins": 5,
    "milestone_losses": 6, "milestone_profit": 0.01,
}


def test_new_keys_in_config_and_preset_lists():
    for k in _NEW:
        assert k in st._CONFIG_KEYS and k in st._PRESET_KEYS


def test_config_round_trip(monkeypatch):
    for k, v in _NEW.items():
        setattr(st.state, k, v)
    st.save_config()
    with open(st.CONFIG_PATH) as f:
        raw = json.load(f)
    assert raw["streak_delay_loss"] == "3:0.5"
    monkeypatch.setattr(st, "state", st.BotState())
    assert st.load_config()
    for k, v in _NEW.items():
        assert getattr(st.state, k) == v, k


def test_disabled_tuple_saved_as_null():
    st.save_config()
    with open(st.CONFIG_PATH) as f:
        raw = json.load(f)
    assert raw["streak_delay_loss"] is None and raw["streakbet_loss"] is None


def test_preset_round_trip(monkeypatch):
    for k, v in _NEW.items():
        setattr(st.state, k, v)
    st.save_preset("p1")
    monkeypatch.setattr(st, "state", st.BotState())
    assert st.load_preset("p1")
    for k, v in _NEW.items():
        assert getattr(st.state, k) == v, k


def test_legacy_config_keeps_defaults():
    with open(st.CONFIG_PATH, "w") as f:
        json.dump({"base_bet": 0.1, "currency": "btc"}, f)
    assert st.load_config()
    s = st.state
    assert s.base_bet == 0.1 and s.recurring is False and s.recurring_delay == 60.0
    assert s.streak_reset_losses is None and s.streak_reset_delay == 600.0
    assert s.streak_delay_loss == (0, 0.0)


def test_load_clamps_delays(caplog):
    with open(st.CONFIG_PATH, "w") as f:
        json.dump({"recurring_delay": 99999, "streak_reset_delay": -5}, f)
    with caplog.at_level(logging.WARNING, logger="stake"):
        assert st.load_config()
    assert st.state.recurring_delay == 86400.0
    assert st.state.streak_reset_delay == 0.0
    assert "clamped" in caplog.text


# ── SIGHUP live reload ────────────────────────────────────
def test_reload_applies_live_keys_only(caplog):
    st.state.game, st.state.base_bet = "limbo", 0.001
    st.save_config()
    with open(st.CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.update(recurring=True, max_bets=5, streak_delay_loss="3:0.5",
               game="dice", base_bet=1.0)
    with open(st.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)

    with caplog.at_level(logging.WARNING, logger="stake"):
        changed = st._reload_live_config()
    assert set(changed) == {"recurring", "max_bets", "streak_delay_loss"}
    s = st.state
    assert s.recurring is True and s.max_bets == 5 and s.streak_delay_loss == (3, 0.5)
    assert s.game == "limbo" and s.base_bet == 0.001
    assert "unsafe" in caplog.text and "game" in caplog.text


def test_reload_no_change_and_missing_config():
    assert st._reload_live_config() == {}
    st.save_config()
    assert st._reload_live_config() == {}


def test_reload_threshold_rearms_milestone():
    st.save_config()
    with open(st.CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["profit_threshold"] = 0.05
    with open(st.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    st._reload_live_config()
    assert st.state.next_profit_milestone == 0.05


def test_sig_reload_swallows_errors(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(st, "_reload_live_config", boom)
    st._sig_reload(None, None)


def test_main_registers_sighup_keeps_usr_signals():
    src = inspect.getsource(st.main)
    assert "signal.SIGHUP, _sig_reload" in src
    assert "signal.SIGUSR1, _sig_pause" in src and "signal.SIGUSR2, _sig_resume" in src


def test_live_reload_keys_exclude_restart_keys():
    for k in ("access_token", "currency", "game", "multiplier_target", "base_bet",
              "strategy", "strategy_key", "loss_mult", "win_mult"):
        assert k not in st._LIVE_RELOAD_KEYS
