"""E0-S2 — fake casino drives the real CLI and TG bet loops.

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q
"""

import sqlite3

import pytest

import stake as st

GAMES = ["limbo", "dice"]
SIX_FOUR = ["win", "loss", "win", "win", "loss", "win", "loss", "win", "win", "loss"]


def _bet_rows(db, session_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT game, amount, multiplier_target, result_value, result_display,"
            " state, profit, balance_after FROM bets WHERE session_id=? ORDER BY rowid",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def _session_row(db, session_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT ended_at, total_bets, wins, losses, profit FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()


def _assert_rows_match(rows, fake, game):
    assert len(rows) == len(fake.bets)
    for row, bet in zip(rows, fake.bets):
        g, amount, _mt, _rv, _rd, state, profit, bal = row
        assert g == game
        assert amount == pytest.approx(bet["amount"])
        assert state == bet["outcome"]
        assert profit == pytest.approx(bet["profit"])
        assert bal == pytest.approx(bet["balance"])


# ── CLI ───────────────────────────────────────────────────
@pytest.mark.parametrize("game", GAMES)
def test_cli_ten_bets(game, fake_casino, cli_state, run_cli):
    fake_casino.queue(*SIX_FOUR)
    cli_state(game)
    s = run_cli()

    assert not fake_casino.exhausted
    assert s.status == "STOPPED: Max bets reached: 10"
    assert (s.total_bets, s.wins, s.losses) == (10, 6, 4)
    assert s.profit == pytest.approx(fake_casino.profit)
    assert s.current_balance == pytest.approx(fake_casino.balance)
    _assert_rows_match(_bet_rows(st.DB_PATH, s.session_id), fake_casino, game)
    ended, total, wins, losses, profit = _session_row(st.DB_PATH, s.session_id)
    assert ended and (total, wins, losses) == (10, 6, 4)
    assert profit == pytest.approx(fake_casino.profit)


@pytest.mark.parametrize("game", GAMES)
def test_cli_insufficient_balance_stops(game, fake_casino, cli_state, run_cli):
    fake_casino.queue("win", "loss", "win", "insufficient_balance", "win")
    cli_state(game)
    s = run_cli()

    assert s.status == "STOPPED: Insufficient balance"
    assert s.total_bets == 3
    assert fake_casino.bet_posts == 4           # no retry after the refusal
    assert len(_bet_rows(st.DB_PATH, s.session_id)) == 3
    assert _session_row(st.DB_PATH, s.session_id)[0]


@pytest.mark.parametrize("game", GAMES)
def test_cli_recovers_from_429_and_timeout(game, fake_casino, cli_state, run_cli, sleeps):
    script = list(SIX_FOUR)
    script.insert(2, "http_429")
    script.insert(5, "timeout")
    fake_casino.queue(*script)
    cli_state(game)
    s = run_cli()

    assert not fake_casino.exhausted
    assert (s.total_bets, s.wins, s.losses) == (10, 6, 4)
    assert fake_casino.bet_posts == 12
    assert sleeps.count(2.0) == 2               # one backoff per error, reset after success
    assert s.current_balance == pytest.approx(fake_casino.balance)


# ── TG ────────────────────────────────────────────────────
@pytest.mark.parametrize("game", GAMES)
def test_tg_ten_bets(game, fake_casino, tg_engine_factory, run_tg):
    fake_casino.queue(*SIX_FOUR)
    e = run_tg(tg_engine_factory(game))

    assert not fake_casino.exhausted
    assert e.stops and "10" in e.stops[0]
    assert (e.total_bets, e.wins, e.losses) == (10, 6, 4)
    assert e.profit == pytest.approx(fake_casino.profit)
    assert e.current_balance == pytest.approx(fake_casino.balance)
    _assert_rows_match(_bet_rows(e.db_path, e.session_id), fake_casino, game)
    ended, total, wins, losses, _ = _session_row(e.db_path, e.session_id)
    assert ended and (total, wins, losses) == (10, 6, 4)


@pytest.mark.parametrize("game", GAMES)
def test_tg_insufficient_balance_stops(game, fake_casino, tg_engine_factory, run_tg):
    fake_casino.queue("win", "loss", "win", "insufficient_balance", "win")
    e = run_tg(tg_engine_factory(game))

    assert e.total_bets == 3
    assert "Insufficient balance" in e.stop_reason
    assert e.errors and "Insufficient balance" in e.errors[0]
    assert fake_casino.bet_posts == 4
    assert len(_bet_rows(e.db_path, e.session_id)) == 3


@pytest.mark.parametrize("game", GAMES)
def test_tg_recovers_from_429_and_timeout(game, fake_casino, tg_engine_factory, run_tg, sleeps):
    script = list(SIX_FOUR)
    script.insert(2, "http_429")
    script.insert(5, "timeout")
    fake_casino.queue(*script)
    e = run_tg(tg_engine_factory(game))

    assert not fake_casino.exhausted
    assert (e.total_bets, e.wins, e.losses) == (10, 6, 4)
    assert fake_casino.bet_posts == 12
    assert sleeps.count(2.0) == 2


# ── Cloudflare 403 then OK ────────────────────────────────
@pytest.mark.parametrize("game", GAMES)
def test_cli_cf_403_then_ok(game, fake_casino, cli_state, run_cli, sleeps):
    # CLI has no mid-session reauth: a 403 is a plain error, backed off, then the next bet goes.
    fake_casino.queue("cf_403", *SIX_FOUR)
    cli_state(game)
    s = run_cli()

    assert not fake_casino.exhausted
    assert (s.total_bets, s.wins, s.losses) == (10, 6, 4)
    assert fake_casino.bet_posts == 11
    assert fake_casino.gql_calls("AuthCheck") == 0
    assert sleeps.count(2.0) == 1
    assert s.current_balance == pytest.approx(fake_casino.balance)


@pytest.mark.parametrize("game", GAMES)
def test_tg_cf_403_reauths_and_retries(game, fake_casino, tg_engine_factory, run_tg, sleeps):
    fake_casino.queue("win", "cf_403", *SIX_FOUR[1:])
    e = run_tg(tg_engine_factory(game))

    assert not fake_casino.exhausted
    assert (e.total_bets, e.wins, e.losses) == (10, 6, 4)
    assert fake_casino.bet_posts == 11
    # start() AuthCheck + one _cf_reauth AuthCheck on the first rotation profile
    assert fake_casino.gql_calls("AuthCheck") == 2
    assert e.impersonations[-1] == "chrome"
    assert 2.0 not in sleeps                    # same bet retried inline, no backoff
    assert e.current_balance == pytest.approx(fake_casino.balance)
