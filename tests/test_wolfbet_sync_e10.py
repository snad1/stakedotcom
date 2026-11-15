"""WolfBet sync: stakedotcom run-wide profit increment (CLI 1.15.0 / TG 1.13.0).

Mirrors WolfBet CLI v2.37.0 (`profit_increment_scope`, `0b7e71a`) and TG
v1.21.0 (`58d6e4d`). Scope "run" measures the profit since the run started
(DB sum of the run's sessions plus the live session), keeps the milestone
across recurring cycles and persists the bumps in a run file.

Reference tests: wolfbet/tests/test_phase10b_run_increment.py (CLI) and
wolfbet/tests/test_tg_run_increment.py (TG).

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q
"""

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import stake as st
import tg.handlers as h
from tg import engine as tg_engine
from tg.config import CONFIG_KEYS
from tg.database import user_db_path

_ATTRS = ("profit", "base_bet", "current_bet", "profit_increment", "profit_threshold",
          "next_profit_milestone", "profit_increment_scope", "run_first_session",
          "run_since", "run_bumps", "run_profit_done", "lane_share", "lane_split",
          "lane_run_profit", "session_id", "start_balance", "current_balance")


@pytest.fixture(autouse=True)
def _restore_state():
    """Each test starts from a fresh BotState and leaves the module's one alone."""
    saved = {a: getattr(st.state, a, None) for a in _ATTRS}
    st.state = st.BotState()
    yield
    for a, v in saved.items():
        setattr(st.state, a, v)
    st.state = st.BotState()


def _seed(rows):
    """rows: [(id, profit, lane)]"""
    st.init_db()
    conn = sqlite3.connect(st.DB_PATH)
    for sid, profit, lane in rows:
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, "
            "multiplier, total_bets, wins, losses, profit, wagered, lane) "
            "VALUES (?, '2026-11-15T10:00:00', 'usdt', 'limbo', 'Flat Bet', 0.0001, 2.0, "
            "4, 2, 2, ?, 0.001, ?)", (sid, profit, lane))
    conn.commit()
    conn.close()


def _cfg(scope, base=0.0001, inc=0.0001, thr=0.01):
    s = st.state
    s.profit_increment_scope, s.base_bet, s.current_bet = scope, base, base
    s.profit_increment, s.profit_threshold = inc, thr
    s.next_profit_milestone = thr
    s.profit = 0.0


def _run_file(lane=None):
    return st.run_path(st.RUN_PATH, lane or st.state.lane)


# ── CLI: session scope unchanged ─────────────────────────
def test_session_scope_uses_session_profit():
    _cfg("session")
    st.state.run_profit_done = 5.0          # ignored in session scope
    st.state.profit = 0.005
    st._check_profit_increment()
    assert st.state.base_bet == pytest.approx(0.0001)
    st.state.profit = 0.01
    st._check_profit_increment()
    assert st.state.base_bet == pytest.approx(0.0002)
    assert st.state.next_profit_milestone == pytest.approx(0.02)


def test_session_scope_keeps_milestone_reset_in_recurring():
    src = open(st.__file__).read()
    body = src[src.index("def _reset_session_for_recurring"):src.index("def betting_loop")]
    assert 'state.profit_increment_scope != "run"' in body
    assert "state.run_profit_done += state.profit" in body


def test_init_run_noop_in_session_scope():
    _cfg("session")
    st._init_run()
    st._attach_run(7)
    assert st.state.run_first_session is None
    assert not os.path.exists(_run_file())


# ── CLI: run scope ───────────────────────────────────────
def test_new_run_created_at_first_session():
    _cfg("run")
    st._init_run()
    assert st.state.run_first_session is None and st.state.run_bumps == 0
    st._attach_run(12)
    data = json.load(open(_run_file()))
    assert data["first_session"] == 12 and data["bumps"] == 0 and data["started_at"]


def test_run_profit_sums_lane_sessions_and_bumps_persist():
    _seed([(1, 0.5, 1), (5, 0.004, 1), (6, 0.003, 1), (7, 0.9, 2)])
    json.dump({"first_session": 5, "started_at": "x", "bumps": 0}, open(_run_file(), "w"))
    _cfg("run")
    st._init_run()
    assert st.state.run_profit_done == pytest.approx(0.007)  # #1 before the run, #7 other lane
    st.state.profit = 0.002
    st._check_profit_increment()
    assert st.state.base_bet == pytest.approx(0.0001)        # 0.009 < 0.01
    st.state.profit = 0.003                                  # run total 0.010
    st._check_profit_increment()
    assert st.state.base_bet == pytest.approx(0.0002)
    assert st.state.next_profit_milestone == pytest.approx(0.02)
    assert json.load(open(_run_file()))["bumps"] == 1


def test_resume_restores_bumps_on_base_bet():
    _seed([(3, 0.025, 1)])
    json.dump({"first_session": 3, "started_at": "x", "bumps": 2}, open(_run_file(), "w"))
    _cfg("run")
    st._init_run()
    assert st.state.base_bet == pytest.approx(0.0003)
    assert st.state.current_bet == pytest.approx(0.0003)
    assert st.state.next_profit_milestone == pytest.approx(0.03)
    st._attach_run(4)                        # an existing run keeps its start
    assert json.load(open(_run_file()))["first_session"] == 3


def test_stale_run_file_after_db_reset_starts_new_run():
    _seed([(1, 0.1, 1)])
    json.dump({"first_session": 5000, "started_at": "x", "bumps": 4}, open(_run_file(), "w"))
    _cfg("run")
    st._init_run()
    assert st.state.run_first_session is None and st.state.run_bumps == 0
    assert st.state.base_bet == pytest.approx(0.0001)


def test_lane_run_file_and_filter():
    _seed([(2, 0.01, 1), (3, 0.02, 2)])
    st.state.lane = 2
    json.dump({"first_session": 2, "started_at": "x", "bumps": 0},
              open(_run_file(2), "w"))
    _cfg("run")
    st._init_run()
    assert st.state.run_first_session == 2
    assert st.state.run_profit_done == pytest.approx(0.02)


def test_split_base_bet_gets_the_bump():
    """With lane_split=bet the bump sits on top of the lane's split base."""
    _seed([(3, 0.05, 2)])
    st.state.lane = 2
    json.dump({"first_session": 3, "started_at": "x", "bumps": 1}, open(_run_file(2), "w"))
    _cfg("run", base=0.0005)                 # this lane's share of a 0.001 base
    st._init_run()
    assert st.state.base_bet == pytest.approx(0.0006)


def test_progress_row_uses_run_total():
    row = st._build_config_progress_row({
        "profit_threshold": 0.01, "profit_increment": 0.0001,
        "next_profit_milestone": 0.02, "profit": 0.0,
        "profit_increment_scope": "run", "run_profit_total": 0.015})
    assert "RunMs" in row and "50%" in row


def test_status_shows_run_profit_row():
    _cfg("run")
    st.state.run_first_session, st.state.run_bumps = 9, 2
    st.state.run_profit_done = 0.025
    st._save_state_file()
    with st.console.capture() as cap:
        st.cmd_status()
    out = cap.get()
    assert "Run Profit" in out and "since #9" in out and "2 bump(s)" in out


# ── CLI: config, mutation, reset ─────────────────────────
def test_config_set_scope_stored_and_validated(capsys):
    st.state.base_bet = 0.0001
    st.save_config()
    st.cmd_config_set(["piscope=run"])
    assert json.load(open(st.CONFIG_PATH))["profit_increment_scope"] == "run"
    with pytest.raises(SystemExit) as e:
        st.cmd_config_set(["profit_increment_scope=forever"])
    assert e.value.code == 2


@pytest.mark.parametrize("key,val", [("profit_increment", "0.0"), ("profit_increment", "-1"),
                                     ("profit_threshold", "0.0"), ("profit_threshold", "none")])
def test_mutation_zero_means_off(key, val):
    _cfg("run")
    st._apply_mutation({key: val})
    assert getattr(st.state, key) is None
    assert not st._increment_on()


def test_zero_increment_never_bumps_or_counts():
    _cfg("run", inc=0.0)
    st.state.run_first_session = 1
    st.state.run_profit_done = 1.0
    st._check_profit_increment()
    assert st.state.run_bumps == 0 and st.state.base_bet == pytest.approx(0.0001)


def test_run_reset_removes_files():
    st.init_db()
    for k in (1, 3):
        open(st.run_path(st.RUN_PATH, k), "w").write("{}")
    assert st.cmd_run_reset() == 2
    assert not os.path.exists(_run_file())
    open(st.run_path(st.RUN_PATH, 2), "w").write("{}")
    assert st.cmd_run_reset(2) == 1


@pytest.mark.parametrize("inc,thr,scope", [(None, 0.01, "run"), (0.0001, None, "run"),
                                           (0.0001, 0.01, "session")])
def test_init_run_off_removes_run_file(inc, thr, scope):
    _seed([(3, 0.05, 1)])
    json.dump({"first_session": 3, "started_at": "x", "bumps": 2}, open(_run_file(), "w"))
    _cfg(scope, inc=inc, thr=thr)
    st._init_run()
    st._attach_run(4)
    assert not os.path.exists(_run_file())
    assert st.state.run_first_session is None and st.state.run_bumps == 0
    assert st.state.base_bet == pytest.approx(0.0001)       # old bumps never come back


def _write_cfg(**vals):
    st.save_config()
    data = json.load(open(st.CONFIG_PATH))
    data.update(vals)
    json.dump(data, open(st.CONFIG_PATH, "w"))


def test_live_reload_increment_off_ends_run():
    _cfg("run")
    st._attach_run(5)
    assert os.path.exists(_run_file())
    _write_cfg(profit_increment_scope="run", profit_increment=None,
               profit_threshold=0.01)
    st._reload_live_config()
    assert not os.path.exists(_run_file()) and st.state.run_first_session is None


def test_live_reload_increment_on_starts_run():
    _cfg("run", inc=None)
    st.state.session_id = 8
    _write_cfg(profit_increment_scope="run", profit_increment=0.0001,
               profit_threshold=0.01)
    st._reload_live_config()
    assert st.state.run_first_session == 8
    assert json.load(open(_run_file()))["first_session"] == 8


def test_bump_never_touches_cloudflare_or_http(monkeypatch):
    """A base-bet bump is a local number: no CF re-auth, no client rebuild."""
    def boom(*a, **k):
        raise AssertionError("the bump must not rebuild the client")
    monkeypatch.setattr(st, "_recreate_http", boom)
    monkeypatch.setattr(st, "_cf_reauth", boom)
    _cfg("run")
    st.state.run_first_session, st.state.run_profit_done = 1, 0.05
    st._check_profit_increment()
    assert st.state.run_bumps == 1


def test_one_merged_increment_helper():
    src = open(st.__file__).read()
    loop = src[src.index("def betting_loop"):]
    assert loop.count("_check_profit_increment()") == 1
    assert "state.base_bet += state.profit_increment" not in loop


# ── TG ───────────────────────────────────────────────────
def test_tg_config_key_present():
    assert "profit_increment_scope" in CONFIG_KEYS


def test_tg_run_created_at_first_session(tg_engine_factory, run_tg):
    e = run_tg(tg_engine_factory(max_bets=1, profit_increment_scope="run",
                                profit_increment=0.0001, profit_threshold=0.01))
    run_file = os.path.join(os.path.dirname(e.db_path), "run.json")
    data = json.load(open(run_file))
    assert data["first_session"] == e.session_id and data["bumps"] == 0


def test_tg_session_scope_writes_no_run_file(tg_engine_factory, run_tg):
    e = run_tg(tg_engine_factory(max_bets=1, profit_increment=0.0001,
                                profit_threshold=0.01))
    assert e.profit_increment_scope == "session"
    assert not os.path.exists(os.path.join(os.path.dirname(e.db_path), "run.json"))


def test_tg_run_total_and_bump_persist(tg_engine_factory, run_tg):
    e = run_tg(tg_engine_factory(max_bets=1, profit_increment_scope="run",
                                profit_increment=0.0001, profit_threshold=0.01))
    e.run_first_session = e.session_id
    e._run_others, e.profit = 0.009, 0.002
    base = e.base_bet
    assert e._maybe_profit_increment() is True          # 0.011 >= 0.01
    assert e.base_bet == pytest.approx(round(base + 0.0001, 8))
    assert e.run_bumps == 1
    run_file = os.path.join(os.path.dirname(e.db_path), "run.json")
    json.dump({"first_session": e.run_first_session, "started_at": "x", "bumps": 1},
              open(run_file, "w"))
    e.run_bumps, e.next_profit_milestone = 0, 0.01
    e._run_sync()                                        # another slot's bump
    assert e.run_bumps == 1
    assert e.next_profit_milestone == pytest.approx(0.02)


def test_tg_resume_does_not_stack_bumps_twice(tg_engine_factory):
    """The snapshot base already holds the bumps; _init_run subtracts them back out."""
    e = tg_engine_factory(max_bets=1, profit_increment_scope="run",
                          profit_increment=0.0001, profit_threshold=0.01,
                          base_bet=0.0003)
    run_file = os.path.join(os.path.dirname(e.db_path), "run.json")
    tg_engine.init_db(e.db_path)
    json.dump({"first_session": 1, "started_at": "x", "bumps": 2}, open(run_file, "w"))
    e._init_run(resumed=True)
    assert e._run_base_origin == pytest.approx(0.0001)
    assert e.base_bet == pytest.approx(0.0003)
    assert e.next_profit_milestone == pytest.approx(0.03)


def test_tg_tweak_zero_ends_the_run(tg_engine_factory, run_tg):
    e = run_tg(tg_engine_factory(max_bets=1, profit_increment_scope="run",
                                profit_increment=0.0001, profit_threshold=0.01))
    run_file = os.path.join(os.path.dirname(e.db_path), "run.json")
    assert os.path.exists(run_file)
    asyncio.run(e.mutate({"profit_increment": None}))
    assert not os.path.exists(run_file) and e.run_first_session is None
    assert e.profit_increment is None


def test_tg_status_reports_run_keys(tg_engine_factory, run_tg):
    e = run_tg(tg_engine_factory(max_bets=1, profit_increment_scope="run",
                                profit_increment=0.0001, profit_threshold=0.01))
    s = e.get_status()
    assert s["profit_increment_scope"] == "run"
    assert s["run_first_session"] == e.session_id
    assert s["run_bumps"] == 0 and "run_profit_total" in s


# ── TG handlers ──────────────────────────────────────────
def _update(uid, replies, **kkw):
    async def reply_text(text, **k):
        replies.append(text)
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           message=SimpleNamespace(reply_text=reply_text))


def _cmd(fn, uid, *args):
    replies = []
    asyncio.run(fn(_update(uid, replies), SimpleNamespace(args=list(args))))
    return replies


def test_tg_set_piscope_validates_and_stores():
    assert "must be `session` or `run`" in _cmd(h.cmd_set, 21, "piscope", "forever")[0]
    _cmd(h.cmd_set, 21, "piscope", "run")
    assert h.load_user_config(21)["profit_increment_scope"] == "run"


def test_tg_set_zero_increment_means_off():
    _cmd(h.cmd_set, 22, "pi", "0.0001")
    _cmd(h.cmd_set, 22, "pi", "0")
    assert h.load_user_config(22)["profit_increment"] is None


def test_tg_tweak_refuses_live_scope_change(tg_engine_factory, run_tg, monkeypatch):
    e = run_tg(tg_engine_factory(max_bets=1))
    monkeypatch.setattr(h, "_resolve_engine", lambda uid, args: (1, e, None))
    replies = _cmd(h.cmd_tweak, 23, "piscope", "run")
    assert "applies on the next session" in replies[0]
    assert e.profit_increment_scope == "session"


def _callback(uid, data, replies):
    async def answer():
        pass

    async def edit_message_text(text, **k):
        replies.append(text)
    return SimpleNamespace(callback_query=SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=uid), answer=answer,
        edit_message_text=edit_message_text, message=None))


def test_tg_runreset_refuses_while_running(monkeypatch):
    monkeypatch.setitem(h.active_engines, 24, {1: SimpleNamespace(running=True)})
    replies = []
    asyncio.run(h.cmd_runreset(_update(24, replies), SimpleNamespace(args=[])))
    assert "Stop your sessions first" in replies[0]
    h.active_engines.pop(24, None)


def test_tg_runreset_confirm_and_cancel(monkeypatch):
    run_file = h._run_file_path(25)
    os.makedirs(os.path.dirname(run_file), exist_ok=True)
    open(run_file, "w").write("{}")
    replies = []
    asyncio.run(h.cmd_runreset(_update(25, replies), SimpleNamespace(args=[])))
    assert "Reset the run total?" in replies[0]

    replies.clear()
    asyncio.run(h.callback_handler(_callback(25, "runreset_cancel", replies),
                                   SimpleNamespace()))
    assert "kept" in replies[0] and os.path.exists(run_file)

    replies.clear()
    asyncio.run(h.callback_handler(_callback(25, "runreset_confirm", replies),
                                   SimpleNamespace()))
    assert "reset" in replies[0].lower() and not os.path.exists(run_file)
