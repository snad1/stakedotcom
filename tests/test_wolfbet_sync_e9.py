"""WolfBet sync E9-S4: stakedotcom CLI lane money split (CLI v1.14.0).

Mirrors WolfBet v2.36.0 (`lane_split`, `6174b96`). Most of it shipped in
1.13.0 and is tested in test_wolfbet_sync_e5_lanes.py (modes, override kept,
round-to-0, save keeps the full bet, basebet_pct divided, live reload,
config-set / config-show, chain warning). This file adds the stake adapt:
a split bet below MIN_BET stays full (logged), for base_bet and basebet_pct.
Reference test: wolfbet/tests/test_phase10_lanes.py; ported shape: goated E9-S2.

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q
"""

import json
import logging
from pathlib import Path

import pytest

import stake as st


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
    cli_state(base_bet=0.0003)              # 0.0003 / 4 = 0.000075 < MIN_BET 0.0001
    st.state.running = False
    st.save_config()
    return Path(st.CONFIG_PATH)


def _write_lanes(path, base_bet=None, **lanes):
    data = json.loads(Path(path).read_text())
    data["lanes"] = lanes
    if base_bet is not None:
        data["base_bet"] = base_bet
    Path(path).write_text(json.dumps(data))


def _fresh_load(lane=1):
    st.state = st.BotState()
    st.state.lane = lane
    assert st.load_config()
    return st.state


def test_min_bet_is_stake_minimum():
    assert st.MIN_BET == 0.0001


# ── base_bet split vs MIN_BET ──────────────────────────────────────────

@pytest.mark.parametrize("mode", ["bet", "both"])
def test_split_below_min_bet_keeps_full(saved_config, mode, caplog):
    _write_lanes(saved_config, count=4, split=mode)
    with caplog.at_level(logging.WARNING, logger="stake"):
        s = _fresh_load(2)
    assert s.base_bet == 0.0003 and s.current_bet == 0.0003
    assert "below the minimum bet" in caplog.text


def test_split_at_or_above_min_bet_divides(saved_config, caplog):
    _write_lanes(saved_config, count=3, split="bet")         # 0.0003 / 3 = 0.0001
    with caplog.at_level(logging.WARNING, logger="stake"):
        s = _fresh_load(1)
    assert s.base_bet == pytest.approx(0.0001)
    assert "below the minimum bet" not in caplog.text


def test_below_min_full_bet_saved_unchanged(saved_config):
    _write_lanes(saved_config, count=4, split="bet")
    _fresh_load(1)
    st.save_config()
    assert json.loads(saved_config.read_text())["base_bet"] == 0.0003


def test_override_below_min_kept_as_is(saved_config):
    _write_lanes(saved_config, count=4, split="bet", overrides={"2": {"base_bet": 0.0002}})
    assert _fresh_load(2).base_bet == 0.0002


def test_live_reload_below_min_keeps_full_no_warning(saved_config, caplog):
    _write_lanes(saved_config, count=4, split="bet")
    s = _fresh_load(2)
    with caplog.at_level(logging.WARNING, logger="stake"):
        changed = st._reload_live_config()
    assert "base_bet" not in changed and s.base_bet == 0.0003
    assert "base_bet changed" not in caplog.text


# ── basebet_pct split vs MIN_BET ───────────────────────────────────────

def test_basebet_pct_below_min_keeps_full(cli_state, caplog):
    # 10 x 0.00003 = 0.0003; / 4 = 0.000075 < MIN_BET
    s = cli_state(basebet_pct=0.00003, current_balance=10.0, lanes_count=4, lane_split="both")
    with caplog.at_level(logging.WARNING, logger="stake"):
        assert st._apply_basebet_pct()
    assert s.base_bet == pytest.approx(0.0003) and s.current_bet == pytest.approx(0.0003)
    assert "below the minimum bet" in caplog.text


def test_basebet_pct_above_min_divided(cli_state):
    s = cli_state(basebet_pct=0.001, current_balance=10.0, lanes_count=2, lane_split="bet")
    assert st._apply_basebet_pct()
    assert s.base_bet == pytest.approx(0.005)


@pytest.mark.parametrize("mode", ["none", "budget"])
def test_basebet_pct_not_divided_without_bet_split(cli_state, mode):
    s = cli_state(basebet_pct=0.001, current_balance=10.0, lanes_count=2, lane_split=mode)
    assert st._apply_basebet_pct()
    assert s.base_bet == pytest.approx(0.01)


# ── modes end to end ───────────────────────────────────────────────────

def test_none_default_full_bet_no_budget(saved_config):
    _write_lanes(saved_config, base_bet=0.01, count=2)
    s = _fresh_load(2)
    assert s.lane_split == "none" and s.base_bet == 0.01
    s.current_balance = 1.0
    st._init_lane_budget()
    assert s.lane_share is None
    s.current_bet = 1e9
    assert st.should_stop() == (False, "")


def test_both_splits_bet_and_budget(saved_config):
    _write_lanes(saved_config, base_bet=0.01, count=2, split="both")
    s = _fresh_load(2)
    assert s.base_bet == pytest.approx(0.005)
    s.current_balance = 4.0
    st._init_lane_budget()
    assert s.lane_share == pytest.approx(2.0)
    s.current_bet = 2.5
    stop, reason = st.should_stop()
    assert stop and reason.startswith("Lane budget exceeded")
