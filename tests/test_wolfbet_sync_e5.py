"""E5-S3 — stakedotcom CLI observability and tooling (WolfBet v2.17.0 .. v2.30.5).

API / cycle metrics, --benchmark (GraphQL UserBalances) / --diagnose,
retention (purge_days, --cleanup, worker), proxy (incl. the saved-proxy-on-
resume fix), async bet writer, risky-config hints, wizard Step 6b keys,
T tweak / _apply_mutation, --config-show / --config-set / --version, monitor
footer and systemd-aware stop. Modelled on WolfBet test_phase3_metrics,
5w/5x/5z, 5i_diagnostics, 5g_proxy, 5h_purge_days, 5j_async_writer,
4a_presets, 4b_mutation, 5d_config_cli, 7_polish and 5m_monitor_ux.

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q
"""

import inspect
import json
import os
import queue
import re
import signal
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

import stake as st

B = st.MIN_BET


def _strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _bet_rows():
    conn = sqlite3.connect(st.DB_PATH)
    try:
        return conn.execute("SELECT session_id, game, amount, state FROM bets ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest.fixture
def writer(monkeypatch):
    """Fresh async-writer globals; always stopped after the test."""
    monkeypatch.setattr(st, "_bet_write_queue", queue.Queue(maxsize=1000))
    monkeypatch.setattr(st, "_bet_writer_stats", {
        "queued": 0, "flushed": 0, "batches": 0, "fallback_sync": 0, "queue_high_watermark": 0})
    monkeypatch.setattr(st, "_bet_writer_thread", None)
    yield
    st._stop_bet_writer(timeout=5.0)


@pytest.fixture
def saved_config(cli_state):
    """A saved config on the fake casino (tokens set, _recreate_http stubbed)."""
    s = cli_state()
    s.running = False
    st.save_config()
    return s


# ── 1. Metrics ────────────────────────────────────────────
def test_metrics_recorded_per_bet(fake_casino, cli_state, run_cli, writer):
    fake_casino.queue(*["win", "loss"] * 3)
    cli_state(max_bets=6)
    s = run_cli()
    assert s.total_bets == 6
    assert s._api_ms_count == 6 and len(s._api_samples) == 6
    assert len(s._cycle_samples) == 6
    assert s.peak_api_ms >= s._last_api_ms >= 0
    assert s._cycle_ms == st._compute_cycle_p50()


def test_api_ms_recorded_on_http_error(fake_casino, cli_state, run_cli):
    fake_casino.queue("http_429", "win")
    cli_state(max_bets=1)
    s = run_cli()
    assert s._api_ms_count == 2          # the 429 reply is a timed round trip too


def test_api_percentiles_and_avgs(cli_state):
    s = cli_state()
    for ms in [10, 20, 30, 40, 1000]:
        st._record_api_ms(ms)
    assert st._compute_api_p50() == 30
    assert st._compute_api_p99() == 1000
    assert st._compute_api_avg() == pytest.approx(220)
    assert st._compute_api_rolling_avg() == pytest.approx(220)
    assert s.peak_api_ms == 1000


def test_metrics_empty_are_zero(cli_state):
    cli_state()
    assert st._compute_api_p50() == st._compute_api_p99() == 0.0
    assert st._compute_cycle_p50() == st._compute_cycle_mean() == 0.0
    assert st._compute_recent_bps() == 0.0
    assert st._compute_theoretical_max_bps() == 0.0
    assert st._compute_efficiency_pct() == 0.0


def test_api_window_is_500(cli_state):
    s = cli_state()
    for _ in range(600):
        st._record_api_ms(5)
    assert len(s._api_samples) == 500 and s._api_ms_count == 600


def test_on_target_pct(cli_state):
    s = cli_state(bet_delay=0.1)
    for ms in (50, 90, 150, 200):
        st._record_api_ms(ms)
    assert st._compute_on_target_pct() == 50.0
    s.bet_delay = 0
    assert st._compute_on_target_pct() is None


def test_cycle_overhead_and_efficiency(cli_state):
    s = cli_state(bet_delay=0.0)
    for _ in range(10):
        st._record_api_ms(40)
        st._record_cycle_ms(50)
    assert s._cycle_ms == 50
    assert s._cycle_overhead_ms == pytest.approx(10)
    assert st._compute_recent_bps() == pytest.approx(20.0)
    assert st._compute_theoretical_max_bps() == pytest.approx(25.0)
    assert st._compute_efficiency_pct() == pytest.approx(80.0)


def test_efficiency_capped_and_delay_floor(cli_state):
    s = cli_state(bet_delay=0.5)
    st._record_api_ms(10)
    st._record_cycle_ms(100)           # faster than the 0.5 s ceiling
    assert st._compute_theoretical_max_bps() == pytest.approx(2.0)
    assert st._compute_efficiency_pct() == 100.0


def test_efficiency_falls_back_to_session_bps(cli_state):
    s = cli_state()
    st._record_api_ms(100)
    s.bets_per_second = 5.0
    assert st._compute_efficiency_pct() == pytest.approx(50.0)


def test_state_file_metric_keys(cli_state):
    cli_state()
    st._record_api_ms(30)
    st._record_cycle_ms(40)
    st._save_state_file()
    with open(st.STATE_PATH) as f:
        d = json.load(f)
    for k in ("api_ms", "api_avg_ms", "api_peak_ms", "api_p50_ms", "api_p99_ms", "on_target_pct",
              "cycle_ms", "overhead_ms", "cycle_p50_ms", "cycle_p99_ms", "cycle_mean_ms",
              "cycle_sample_count", "bets_per_second_recent", "efficiency_pct",
              "theoretical_max_bps", "db_write_mode", "db_writer_stats", "db_writer_queue",
              "purge_days"):
        assert k in d, k
    assert d["api_p50_ms"] == 30 and d["cycle_sample_count"] == 1
    assert not any(k.startswith("rate_") or k.startswith("pipeline") for k in d)


def test_dashboard_shows_metric_rows(cli_state):
    cli_state(bet_delay=0.2)
    st._record_api_ms(30)
    out = _strip_ansi(st.build_dashboard_screen())
    assert "API last 30ms" in out and "p99" in out
    assert "API stable" in out and "on-target" in out
    assert "Efficiency" in out and "Cycle" in out
    assert "Rate limit" not in out
    assert "[T]weak" in out


def test_status_shows_observability(cli_state, capsys):
    cli_state()
    st._record_api_ms(30)
    st._record_cycle_ms(40)
    st._save_state_file()
    st.cmd_status()
    out = capsys.readouterr().out
    assert "API" in out and "p50 30ms" in out
    assert "Cycle" in out and "Efficiency" in out


# ── 2. Benchmark / diagnose ───────────────────────────────
def test_benchmark_uses_user_balances(fake_casino, saved_config, capsys):
    before = fake_casino.bet_posts
    st.cmd_benchmark(5)
    out = capsys.readouterr().out
    assert fake_casino.gql_calls("UserBalances") == 5
    assert fake_casino.bet_posts == before          # read-only
    assert "UserBalances" in out and "Theoretical max BPS from p50" in out


def test_benchmark_clamps_n(fake_casino, saved_config):
    st.cmd_benchmark(1)
    assert fake_casino.gql_calls("UserBalances") == 3


def test_benchmark_429_verdict_red(saved_config, monkeypatch, capsys):
    class R:
        status_code = 429
    class H:
        def post(self, *a, **k):
            return R()
    monkeypatch.setattr(st, "_http", H())
    st.cmd_benchmark(4)
    out = capsys.readouterr().out
    assert "Rate-limited (4/4 got 429)" in out and "Stake is throttling" in out


def test_benchmark_403_hint(saved_config, monkeypatch, capsys):
    class R:
        status_code = 403
    class H:
        def post(self, *a, **k):
            return R()
    monkeypatch.setattr(st, "_http", H())
    st.cmd_benchmark(3)
    assert "got 403 (Cloudflare)" in capsys.readouterr().out


def test_benchmark_no_config_exits(capsys):
    with pytest.raises(SystemExit):
        st.cmd_benchmark(3)


def test_benchmark_proxy_override(saved_config, monkeypatch, fake_casino):
    seen = []
    monkeypatch.setattr(st, "_recreate_http", lambda impersonate="chrome": seen.append(st.state.proxy))
    st.cmd_benchmark(3, proxy="socks5://127.0.0.1:1080")
    assert seen[-1] == "socks5://127.0.0.1:1080"


def test_diagnose_without_state_file(capsys):
    st.cmd_diagnose()
    assert "No running daemon session" in capsys.readouterr().out


def test_diagnose_reads_state_file(cli_state, capsys):
    s = cli_state(bet_delay=0.0)
    s.db_write_mode = "sync"
    for _ in range(5):
        st._record_api_ms(20)
        st._record_cycle_ms(80)
    st._save_state_file()
    st.cmd_diagnose()
    out = capsys.readouterr().out
    assert "Top blocker: CLI overhead" in out
    assert "dbmode=async" in out
    assert "Pipeline" not in out and "Rate limit" not in out


def test_diagnose_network_advice(cli_state, capsys):
    cli_state()
    for _ in range(5):
        st._record_api_ms(250)
        st._record_cycle_ms(260)
    st._save_state_file()
    st.cmd_diagnose()
    out = capsys.readouterr().out
    assert "Network / API latency" in out and "--benchmark" in out


# ── 3. Retention ──────────────────────────────────────────
def _old_session_with_bets(days_old, n_bets=3):
    sid = st._db_start_session()
    ended = (datetime.now() - timedelta(days=days_old)).isoformat()
    conn = sqlite3.connect(st.DB_PATH)
    conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (ended, sid))
    for _ in range(n_bets):
        conn.execute("INSERT INTO bets (session_id, timestamp, game, amount, state) VALUES (?,?,?,?,?)",
                     (sid, ended, "limbo", B, "win"))
    conn.commit()
    conn.close()
    return sid


def test_cleanup_purges_old_sessions_only(cli_state, capsys):
    cli_state()
    _old_session_with_bets(10)
    _old_session_with_bets(1)
    st.cmd_cleanup(5)
    out = capsys.readouterr().out
    assert "Purged 3 bet rows" in out
    assert len(_bet_rows()) == 3
    conn = sqlite3.connect(st.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3   # rows kept
    conn.close()


def test_cleanup_default_uses_purge_days_and_clamps(cli_state, capsys):
    cli_state()
    st.state.purge_days = 30
    st.save_config()
    _old_session_with_bets(20)
    st.cmd_cleanup()                       # 30-day window: nothing
    assert "Nothing to purge (30-day window" in capsys.readouterr().out
    st.cmd_cleanup(99)                     # clamps to 30
    assert "30-day window" in capsys.readouterr().out


def test_cleanup_no_db(capsys):
    st.cmd_cleanup(3)
    assert "No database found" in capsys.readouterr().out


def test_retention_worker_calls_cleanup_live(cli_state, monkeypatch):
    s = cli_state()
    s.purge_days = 7
    calls = []

    def fake_cleanup(path, bet_age_days):
        calls.append((path, bet_age_days))
        s.running = False
        return 0
    monkeypatch.setattr(st, "cleanup_live_bets", fake_cleanup)
    st._retention_worker()
    assert calls == [(st.DB_PATH, 7)]


def test_retention_worker_exits_when_stopped(cli_state, monkeypatch):
    s = cli_state()
    s.running = False
    monkeypatch.setattr(st, "cleanup_live_bets", lambda *a, **k: pytest.fail("no purge"))
    st._retention_worker()


def test_main_startup_purge_and_threads():
    src = inspect.getsource(st.main)
    assert "cleanup_old_bets(DB_PATH, bet_age_days=state.purge_days)" in src
    assert "_retention_worker" in src and "_start_bet_writer()" in src
    assert src.count("_stop_bet_writer(") == 3       # SIGTERM, daemon exit, TUI exit


@pytest.mark.parametrize("raw,want", [(0, 1), (7, 7), (45, 30), ("x", 3)])
def test_purge_days_load_clamp(raw, want):
    with open(st.CONFIG_PATH, "w") as f:
        json.dump({"purge_days": raw}, f)
    st.load_config()
    assert st.state.purge_days == want


# ── 4. Proxy ──────────────────────────────────────────────
@pytest.fixture
def proxy_spy(monkeypatch, fake_casino):
    """_recreate_http spy: records state.proxy at each call, hands back the fake."""
    seen = []

    def spy(impersonate="chrome"):
        seen.append(st.state.proxy)
        st._http = fake_casino.sync_session()
    monkeypatch.setattr(st, "_recreate_http", spy)
    st._http = fake_casino.sync_session()
    return seen


def _save_with_proxy(proxy):
    st.state.access_token = "test-token"
    st.state.currency = "usdt"
    st.state.proxy = proxy
    st.save_config()
    st.state.proxy = ""


def test_load_config_applies_saved_proxy(proxy_spy):
    _save_with_proxy("socks5://10.0.0.1:1080")
    assert st.load_config()
    assert proxy_spy[-1] == "socks5://10.0.0.1:1080"


def test_load_and_connect_resume_uses_saved_proxy(proxy_spy, fake_casino):
    """Regression: saved proxy ignored on --resume / --daemon."""
    _save_with_proxy("http://u:p@10.0.0.2:8080")
    st._load_and_connect()
    assert proxy_spy and proxy_spy[0] == "http://u:p@10.0.0.2:8080"
    assert fake_casino.gql_calls("AuthCheck") >= 1


def test_cli_proxy_beats_saved(proxy_spy):
    _save_with_proxy("socks5://10.0.0.1:1080")
    st._load_and_connect(proxy="socks5://10.9.9.9:1080")
    assert st.state.proxy == "socks5://10.9.9.9:1080"
    assert proxy_spy[-1] == "socks5://10.9.9.9:1080"


class _Stop(BaseException):
    pass


@pytest.mark.parametrize("argv", [["--resume"], ["--daemon"]])
def test_main_resume_and_daemon_apply_saved_proxy(argv, proxy_spy, monkeypatch):
    _save_with_proxy("socks5://10.0.0.3:1080")
    seen_at_connect = []

    def connect():
        seen_at_connect.append(list(proxy_spy))
        raise _Stop()
    monkeypatch.setattr(st, "api_test_connection", connect)
    monkeypatch.setattr(sys, "argv", ["stake.py", *argv])
    with pytest.raises(_Stop):
        st.main()
    assert seen_at_connect[0][-1] == "socks5://10.0.0.3:1080"


def test_main_preset_applies_saved_proxy(proxy_spy, monkeypatch):
    _save_with_proxy("socks5://10.0.0.4:1080")
    st.save_preset("p1")
    seen_at_connect = []

    def connect():
        seen_at_connect.append(st.state.proxy)
        raise _Stop()
    monkeypatch.setattr(st, "api_test_connection", connect)
    monkeypatch.setattr(sys, "argv", ["stake.py", "--preset", "p1"])
    with pytest.raises(_Stop):
        st.main()
    assert seen_at_connect == ["socks5://10.0.0.4:1080"]
    assert proxy_spy[-1] == "socks5://10.0.0.4:1080"


def test_main_rejects_bad_proxy(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["stake.py", "--proxy", "ftp://x:1", "--status"])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert e.value.code == 2


def test_saved_bad_proxy_dropped(caplog):
    with open(st.CONFIG_PATH, "w") as f:
        json.dump({"proxy": "ftp://nope"}, f)
    st.load_config()
    assert st.state.proxy == ""


def test_saved_off_proxy_is_direct():
    with open(st.CONFIG_PATH, "w") as f:
        json.dump({"proxy": "off"}, f)
    st.load_config()
    assert st.state.proxy == ""


def test_no_cffi_proxy_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(st, "_HAS_CFFI", False)
    monkeypatch.setattr(st, "_warned_no_cffi_proxy", False)
    st.state.proxy = "socks5://10.0.0.1:1080"
    with caplog.at_level("WARNING", logger="stake"):
        st._recreate_http()
        st._recreate_http()
    assert caplog.text.count("curl_cffi is not installed") == 1


@pytest.mark.skipif(not st._HAS_CFFI, reason="curl_cffi not installed")
def test_recreate_http_passes_proxy(monkeypatch):
    made = []

    class S:
        def __init__(self, **kw):
            made.append(kw)
    monkeypatch.setattr(st.cffi_requests, "Session", S)
    st.state.proxy = "socks5://10.0.0.1:1080"
    st._recreate_http()
    assert made[-1] == {"impersonate": "chrome", "proxy": "socks5://10.0.0.1:1080"}


def test_mutation_proxy_validates_and_masks(monkeypatch):
    calls = []
    monkeypatch.setattr(st, "_recreate_http", lambda impersonate="chrome": calls.append(1))
    msgs = st._apply_mutation({"proxy": "http://user:secret@10.0.0.1:8080"})
    assert st.state.proxy == "http://user:secret@10.0.0.1:8080" and calls
    assert "secret" not in " ".join(msgs)
    with pytest.raises(ValueError):
        st._apply_mutation({"proxy": "ftp://x:1"})
    st._apply_mutation({"proxy": "off"})
    assert st.state.proxy == ""


def test_no_bare_requests_for_casino_api():
    src = inspect.getsource(st)
    bare = [l for l in src.splitlines() if re.search(r"\brequests\.(get|post|put|delete)\(", l)]
    assert bare and all("FLARESOLVERR_URL" in l for l in bare), bare
    for fn in (st.api_get_balances, st._api_post, st.cmd_benchmark):
        assert "_http.post(" in inspect.getsource(fn)


# ── 5. Async bet writer ───────────────────────────────────
def _result(win=True):
    return {"amount": B, "result_value": 2.5, "result_display": "2.5000x", "is_win": win}


def test_writer_batches_and_drains(cli_state, writer):
    s = cli_state()
    st._start_bet_writer()
    for i in range(40):
        st._db_save_bet_dispatch(_result(i % 2 == 0), 0.0, 10.0)
    st._stop_bet_writer(timeout=5.0)
    rows = _bet_rows()
    assert len(rows) == 40 and rows[0][:2] == (s.session_id, "limbo")
    stats = st._bet_writer_stats
    assert stats["queued"] == 40 and stats["flushed"] == 40
    assert 2 <= stats["batches"] <= 40 and stats["fallback_sync"] == 0


def test_writer_idempotent_start(writer):
    st._start_bet_writer()
    t = st._bet_writer_thread
    st._start_bet_writer()
    assert st._bet_writer_thread is t


def test_overflow_falls_back_to_sync(cli_state, writer, monkeypatch):
    cli_state()
    monkeypatch.setattr(st, "_bet_write_queue", queue.Queue(maxsize=1))
    st._db_save_bet_async(_result(), 0.0, 10.0)      # fills the queue
    st._db_save_bet_async(_result(), 0.0, 10.0)      # overflow -> sync
    assert st._bet_writer_stats["fallback_sync"] == 1
    assert len(_bet_rows()) == 1


def test_sync_mode_bypasses_queue(cli_state, writer):
    s = cli_state()
    s.db_write_mode = "sync"
    st._start_bet_writer()
    st._db_save_bet_dispatch(_result(), 0.0, 10.0)
    assert st._bet_writer_stats["queued"] == 0
    assert len(_bet_rows()) == 1


def test_no_thread_means_sync(cli_state, writer):
    cli_state()
    st._db_save_bet_dispatch(_result(), 0.0, 10.0)
    assert st._bet_writer_stats["queued"] == 0 and len(_bet_rows()) == 1


def test_betting_loop_writes_through_writer(fake_casino, cli_state, run_cli, writer):
    fake_casino.queue("win", "loss", "win")
    cli_state(max_bets=3)
    st._start_bet_writer()
    run_cli()
    st._stop_bet_writer(timeout=5.0)
    assert [r[3] for r in _bet_rows()] == ["win", "loss", "win"]
    assert st._bet_writer_stats["queued"] == 3


def test_db_write_mode_validation_and_load():
    with pytest.raises(ValueError):
        st._apply_mutation({"db_write_mode": "turbo"})
    st._apply_mutation({"db_write_mode": "SYNC"})
    assert st.state.db_write_mode == "sync"
    with open(st.CONFIG_PATH, "w") as f:
        json.dump({"db_write_mode": "bogus"}, f)
    st.load_config()
    assert st.state.db_write_mode == "async"


# ── 6. Risky config ───────────────────────────────────────
def _risk(**kw):
    s = st.state
    s.strategy_key, s.loss_mult, s.base_bet, s.current_balance = "1", 2.0, B, 10.0
    s.multiplier_target = 2.0
    s.max_loss = s.max_profit = s.max_bets = None
    for k, v in kw.items():
        setattr(s, k, v)
    return st._check_risky_config()


def test_risky_clean_config():
    assert _risk(max_bets=100) == []


@pytest.mark.parametrize("sk", ["2", "6"])
def test_risky_doubling_without_max_loss(sk):
    assert any("No `max_loss`" in w for w in _risk(strategy_key=sk))


def test_risky_chain_depth_with_streak_reset():
    w = _risk(strategy_key="6", streak_reset_losses=5, base_bet=1.0, current_balance=7.0)
    assert any("covers a 3-loss chain" in x and "Loss 4 in a row" in x for x in w)
    assert not any("chain" in x for x in _risk(strategy_key="6", streak_reset_losses=5,
                                                base_bet=1.0, current_balance=7.0, max_loss=1.0))
    assert not any("chain" in x for x in _risk(strategy_key="1", streak_reset_losses=5))


def test_martingale_chain_depth():
    assert st.martingale_chain_depth(7.0, 1.0, 2.0) == 3       # 1+2+4 = 7
    assert st.martingale_chain_depth(6.99, 1.0, 2.0) == 2
    assert st.martingale_chain_depth(0, 1.0, 2.0) == 0
    assert st.martingale_chain_depth(10, 1.0, 1.0) == 0


def test_risky_other_hints():
    assert any("Aggressive Martingale" in w for w in _risk(strategy_key="2", loss_mult=3.0, max_loss=1))
    assert any("Anti-Martingale" in w for w in _risk(strategy_key="3", max_bets=1))
    assert any("Strategy 7" in w for w in _risk(strategy_key="7"))
    assert any("Base bet is" in w for w in _risk(base_bet=1.0, max_bets=1))
    assert any("Multiplier 20" in w for w in _risk(multiplier_target=20.0, max_bets=1))
    assert any("Recurring restart" in w for w in _risk(recurring=True))
    assert any("basebet_pct" in w for w in _risk(basebet_pct=0.1, max_bets=1))
    w = _risk(proxy="http://u:secret@1.2.3.4:80", max_bets=1)
    assert any("http://" in x for x in w) and not any("secret" in x for x in w)


def test_confirm_step_shows_risky_panel():
    src = inspect.getsource(st.setup_wizard)
    assert "_check_risky_config()" in src and "Heads-up" in src


# ── 6. Wizard Step 6b + presets ───────────────────────────
def test_wizard_has_advanced_and_preset_steps():
    src = inspect.getsource(st.setup_wizard)
    assert "step_advanced_tuning, step_preset, step_confirm" in src
    assert "Step 8 / 8" in src and "/ 7 --" not in src
    assert "Step 6b / 8" in src and "pipeline" not in src.lower()


def test_new_keys_persist_in_config_and_presets(monkeypatch):
    for k in ("purge_days", "db_write_mode"):
        assert k in st._CONFIG_KEYS and k in st._PRESET_KEYS and k in st._LIVE_RELOAD_KEYS
    assert "proxy" not in st._PRESET_KEYS
    st.state.purge_days, st.state.db_write_mode = 9, "sync"
    st.save_preset("p")
    monkeypatch.setattr(st, "state", st.BotState())
    st.load_preset("p")
    assert (st.state.purge_days, st.state.db_write_mode) == (9, "sync")


def test_reload_picks_up_purge_days():
    st.save_config()
    with open(st.CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg["purge_days"] = 12
    with open(st.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    assert "purge_days" in st._reload_live_config()
    assert st.state.purge_days == 12


# ── 6. _apply_mutation / tweak ────────────────────────────
def test_mutation_aliases_resolve():
    for alias, canon in st._TWEAK_ALIAS.items():
        assert canon in st._TWEAK_PARAMS, alias
    assert "pipeline_depth" not in st._TWEAK_PARAMS


def test_mutation_stop_conditions_and_off():
    st._apply_mutation({"max_profit": "0.5", "max_bets": "100"})
    assert st.state.max_profit == 0.5 and st.state.max_bets == 100
    st._apply_mutation({"max_profit": "off", "max_bets": "none"})
    assert st.state.max_profit is None and st.state.max_bets is None


def test_mutation_streak_tuples():
    st._apply_mutation({"streak_delay_loss": "3:0.5", "streakbet_loss": "4:0.5"})
    assert st.state.streak_delay_loss == (3, 0.5) and st.state.streakbet_loss == (4, 0.5)
    st._apply_mutation({"streakbet_loss": "off"})
    assert st.state.streakbet_loss == (0, 1.0)


def test_mutation_clamps_warn():
    msgs = st._apply_mutation({"recurring_delay": "99999", "purge_days": "0"})
    assert st.state.recurring_delay == 86400.0 and st.state.purge_days == 1
    assert sum(m.startswith("⚠") for m in msgs) == 2


def test_mutation_streak_reset_validation():
    st._apply_mutation({"streak_reset_losses": "5"})
    assert st.state.streak_reset_losses == 5
    with pytest.raises(ValueError):
        st._apply_mutation({"streak_reset_losses": "-1"})
    with pytest.raises(ValueError):
        st._apply_mutation({"max_profit": "abc"})


def test_mutation_recurring_bool():
    st._apply_mutation({"recurring": "on"})
    assert st.state.recurring is True
    st._apply_mutation({"recurring": "off"})
    assert st.state.recurring is False


def test_mutation_base_bet_applies_next_bet_and_min():
    st._apply_mutation({"base_bet": "0.001"})
    assert st.state.base_bet == st.state.current_bet == 0.001
    msgs = st._apply_mutation({"base_bet": "0.00000001"})
    assert st.state.base_bet == B and any(m.startswith("⚠") for m in msgs)
    with pytest.raises(ValueError):
        st._apply_mutation({"base_bet": "-1"})


def test_mutation_multiplier_rederives_dice_target():
    st.state.game, st.state.dice_condition = "dice", "above"
    st._apply_mutation({"multiplier_target": "3"})
    assert st.state.multiplier_target == 3.0 and st.state.dice_target == 67.0
    st.state.dice_condition = "below"
    st._apply_mutation({"multiplier_target": "2"})
    assert st.state.dice_target == 49.5
    with pytest.raises(ValueError):
        st._apply_mutation({"multiplier_target": "1.0"})


def test_mutation_limbo_multiplier_leaves_dice_target():
    st.state.game, st.state.dice_target = "limbo", 50.5
    st._apply_mutation({"multiplier_target": "5"})
    assert st.state.dice_target == 50.5


def test_mutation_profit_threshold_rearms():
    st._apply_mutation({"profit_threshold": "0.01"})
    assert st.state.next_profit_milestone == 0.01


def test_show_tweak_applies_and_restores_pause(cli_state, monkeypatch):
    s = cli_state()
    s.paused = False
    answers = iter(["bogus 1", "delay", "maxbets 42"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    st.show_tweak()
    assert s.max_bets == 42 and s.paused is False


def test_show_tweak_cancel(cli_state, monkeypatch):
    s = cli_state(max_bets=7)
    monkeypatch.setattr("builtins.input", lambda *a: "back")
    st.show_tweak()
    assert s.max_bets == 7


def test_input_handler_t_opens_tweak():
    src = inspect.getsource(st.input_handler)
    assert 'ch == "t"' in src and "show_tweak()" in src


# ── 6. --config-show / --config-set / --version ───────────
def test_parse_config_set_pairs():
    assert st._parse_config_set_pairs(["maxprofit=0.5", "delay = 0.1", "mult=3"]) == {
        "max_profit": "0.5", "bet_delay": "0.1", "multiplier_target": "3"}
    with pytest.raises(ValueError):
        st._parse_config_set_pairs(["nokey"])
    with pytest.raises(ValueError):
        st._parse_config_set_pairs(["game=dice"])


def test_config_show_masks_secrets(saved_config, capsys):
    st.state.proxy = ""
    with open(st.CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.update(access_token="tok_abcdefghijklmnop_SECRET", cookie="session=SECRETCOOKIEVALUE123",
               proxy="http://user:pwsecret@10.0.0.1:8080")
    with open(st.CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    st.cmd_config_show()
    out = capsys.readouterr().out
    assert "SECRET" not in out and "pwsecret" not in out
    assert "purge_days" in out and "db_write_mode" in out and "10.0.0.1" in out


def test_config_show_without_config_exits():
    with pytest.raises(SystemExit) as e:
        st.cmd_config_show()
    assert e.value.code == 1


@pytest.mark.parametrize("pair", ["access_token=x", "lockdown_token=x", "cookie=x"])
def test_config_set_refuses_credentials(pair, saved_config):
    with pytest.raises(SystemExit) as e:
        st.cmd_config_set([pair])
    assert e.value.code == 2


def test_config_set_saves_and_reports(saved_config, capsys):
    st.cmd_config_set(["maxprofit=0.5", "purgedays=40", "mult=3"])
    out = capsys.readouterr().out
    with open(st.CONFIG_PATH) as f:
        cfg = json.load(f)
    assert cfg["max_profit"] == 0.5 and cfg["purge_days"] == 30 and cfg["multiplier_target"] == 3.0
    assert "⚠ purge_days clamped" in out
    assert "need a full restart" in out and "multiplier_target" in out
    assert "Daemon not running" in out


def test_config_set_bad_value_exits(saved_config):
    with pytest.raises(SystemExit) as e:
        st.cmd_config_set(["maxbets=abc"])
    assert e.value.code == 2


def test_config_set_sighups_running_daemon(saved_config, monkeypatch, capsys):
    with open(st.PID_PATH, "w") as f:
        f.write("4242")
    kills = []
    monkeypatch.setattr(st.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    st.cmd_config_set(["delay=0.2"])
    assert kills == [(4242, signal.SIGHUP)]
    assert "reloaded live config" in capsys.readouterr().out


def test_config_set_stale_pid(saved_config, monkeypatch, capsys):
    with open(st.PID_PATH, "w") as f:
        f.write("4242")

    def boom(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(st.os, "kill", boom)
    st.cmd_config_set(["delay=0.2"])
    assert "PID stale" in capsys.readouterr().out


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["stake.py", "--version"])
    with pytest.raises(SystemExit) as e:
        st.main()
    assert e.value.code == 0
    assert f"Stake AutoBot v{st.VERSION}" in capsys.readouterr().out


def test_main_dispatches_new_commands(monkeypatch):
    called = []
    for name in ("cmd_config_show", "cmd_config_set", "cmd_cleanup", "cmd_benchmark", "cmd_diagnose"):
        monkeypatch.setattr(st, name, lambda *a, _n=name, **k: called.append((_n, a, k)))
    for argv in (["--config-show"], ["--config-set", "delay=0"], ["--cleanup"], ["--cleanup", "5"],
                 ["--benchmark"], ["--diagnose"]):
        monkeypatch.setattr(sys, "argv", ["stake.py", *argv])
        st.main()
    assert [c[0] for c in called] == ["cmd_config_show", "cmd_config_set", "cmd_cleanup",
                                      "cmd_cleanup", "cmd_benchmark", "cmd_diagnose"]
    assert called[2][1] == (None,) and called[3][1] == (5,)
    assert called[4][1] == (20,)


def test_no_stale_version_text():
    src = inspect.getsource(st)
    assert "v1.2" not in src


# ── 6. Monitor footer + systemd-aware stop ────────────────
def test_monitor_footer_hints(cli_state):
    cli_state()
    st._save_state_file()
    with open(st.STATE_PATH) as f:
        d = json.load(f)
    out = _strip_ansi(st._build_monitor_screen(d, [], True))
    assert "--config-set KEY=VALUE" in out and "--stats / --analytics" in out
    assert "[S]top daemon" in out


def test_stop_daemon_uses_systemctl_when_managed(monkeypatch):
    import subprocess
    runs, kills = [], []
    monkeypatch.setattr(st, "_is_systemd_managed", lambda pid: True)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: runs.append(cmd))
    monkeypatch.setattr(st.os, "kill", lambda pid, sig: kills.append(pid))
    assert st._stop_daemon(1234) == "systemd"
    assert runs == [["systemctl", "--user", "stop", "stake"]] and kills == []


def test_stop_daemon_sigterm_otherwise(monkeypatch):
    kills = []
    monkeypatch.setattr(st, "_is_systemd_managed", lambda pid: False)
    monkeypatch.setattr(st.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    assert st._stop_daemon(1234) == "signal"
    assert kills == [(1234, signal.SIGTERM)]


def test_is_systemd_managed_reads_cgroup(monkeypatch, tmp_path):
    assert st._is_systemd_managed(None) is False
    assert st._is_systemd_managed(999999999) is False
    import builtins
    real_open = builtins.open
    texts = {"/proc/1/cgroup": "0::/user.slice/app.slice/stake.service\n",
             "/proc/2/cgroup": "0::/user.slice/app.slice/stake-tg.service\n"}

    def fake_open(path, *a, **k):
        if path in texts:
            p = tmp_path / path.replace("/", "_")
            p.write_text(texts[path])
            return real_open(p, *a, **k)
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)
    assert st._is_systemd_managed(1) is True
    assert st._is_systemd_managed(2) is False


def test_monitor_stop_goes_through_stop_daemon():
    assert "_stop_daemon(pid)" in inspect.getsource(st.cmd_monitor)
