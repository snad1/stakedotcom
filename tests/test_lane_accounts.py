"""Stake's login in the host vault, lanes that bet from their own account, and a Cloudflare
session per proxy.

Reference: wolfbet/tests/test_phase15_vault.py and shuffle/tests/test_lane_accounts.py. The vault
lives in each test's tmp dir (conftest points CASINO_VAULT_DIR and CASINO_VAULT_KEY_FILE there).
"""

import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest

import stake as go
from shared.core.vault import Vault

# Taken before conftest swaps the solvers for stubs.
_REAL_FLARESOLVERR = go._solve_cloudflare
_REAL_NODRIVER = go._solve_cf_nodriver

TOKEN = "stake-main-token-0123456789abcdef"
LOCKDOWN = "lockdown-main-0123"
COOKIE = "session=main-abc; locale=en"
MAIN = {"access_token": TOKEN, "lockdown_token": LOCKDOWN, "cookie": COOKIE}
FRIEND = {"access_token": "stake-friend-token-fedcba9876543210",
          "lockdown_token": "lockdown-friend-77", "cookie": "session=friend-xyz; locale=de"}
PROXY = "socks5://friend:pw-secret@10.0.0.9:1080"


@pytest.fixture(autouse=True)
def _lane_paths(monkeypatch):
    monkeypatch.setattr(go, "_BASE_STATE_PATH", go.STATE_PATH)
    monkeypatch.setattr(go, "_BASE_PID_PATH", go.PID_PATH)
    fmt = go._log_handler.formatter
    yield
    go._log_handler.setFormatter(fmt)


def _vault():
    return Vault("stake")


def _write(data: dict) -> Path:
    path = Path(go.CONFIG_PATH)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return path


def _raw() -> dict:
    return json.loads(Path(go.CONFIG_PATH).read_text())


def _base(**extra) -> dict:
    return {**MAIN, "currency": "usdt", "game": "limbo", "base_bet": 0.01,
            "multiplier_target": 2.0, **extra}


def _load(lane=1):
    go.state = go.BotState()
    go.state.lane = lane
    assert go.load_config()
    return go.state


def _items(**lanes) -> dict:
    return {"split": "budget", "next_id": max(int(k) for k in lanes) + 1,
            "items": {k: {"name": f"Lane {k}", "enabled": True, "overrides": v}
                      for k, v in lanes.items()}}


def _set(pairs) -> int:
    """stake's config-set exits on a refusal and returns on success: give one exit code."""
    try:
        go.cmd_config_set(pairs)
    except SystemExit as e:
        return e.code
    return 0


def _stdin(monkeypatch, *lines):
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(f"{line}\n" for line in lines)))


def _login(s) -> dict:
    return {f: getattr(s, f) for f in go.ACCOUNT_FIELDS}


# ── the main login moves into the vault ─────────────────────────────────

def test_lane_one_moves_the_login_in_and_keeps_one_private_backup():
    path = _write(_base())
    old = path.read_text()
    _load()
    go._sync_vault_account()
    assert _vault().account("default") == MAIN and _vault().info("default").last4 == "cdef"
    assert not set(go.ACCOUNT_FIELDS) & set(_raw())
    backup = Path(go.CONFIG_PATH + ".pre-vault")
    assert backup.read_text() == old and oct(backup.stat().st_mode & 0o777) == "0o600"
    assert _login(_load()) == MAIN                # vault only from now on
    go.save_config()
    assert not set(go.ACCOUNT_FIELDS) & set(_raw())


def test_a_broken_vault_keeps_the_config_copy(caplog):
    _write(_base())
    kek = Path(os.environ["CASINO_VAULT_KEY_FILE"])
    kek.parent.mkdir(parents=True)
    kek.write_bytes(b"x" * 32)
    os.chmod(kek, 0o644)
    with caplog.at_level(logging.WARNING, logger="stake"):
        s = _load()
        go._sync_vault_account()
    assert _login(s) == MAIN and _raw()["access_token"] == TOKEN and _raw()["cookie"] == COOKIE
    assert "Key vault unavailable" in caplog.text and TOKEN not in caplog.text
    assert not Path(go.CONFIG_PATH + ".pre-vault").exists()


def test_lanes_two_and_up_never_write_the_vault():
    _write(_base(lanes=_items(**{"1": {}, "2": {}})))
    s = _load(2)
    go._sync_vault_account()
    assert _login(s) == MAIN and _vault().info("default") is None
    assert _raw()["access_token"] == TOKEN


# ── a lane on its own account ───────────────────────────────────────────

def _friend_vault():
    v = _vault()
    v.put_account("default", MAIN, "main", primary="access_token")
    v.put_account("friend-a", FRIEND, "Friend A", primary="access_token")
    v.put("friend-px", PROXY, "Friend proxy", kind="proxy")


def test_a_lane_bets_from_its_own_login_currency_and_proxy():
    _friend_vault()
    _write(_base(lanes=_items(**{"1": {}, "3": {"key_ref": "friend-a", "currency": "trx",
                                              "proxy_ref": "friend-px"}})))
    s = _load(3)
    assert (_login(s), s.currency, s.proxy) == (FRIEND, "trx", PROXY)
    assert s.account_problem is None and s.split_lanes == 1
    one = _load(1)
    assert (_login(one), one.currency, one.proxy) == (MAIN, "usdt", "")


def test_split_counts_only_lanes_on_the_same_account():
    _friend_vault()
    _write(_base(lanes=_items(**{"1": {}, "2": {}, "3": {"key_ref": "friend-a"}})))
    assert _load(1).split_lanes == 2 and _load(3).split_lanes == 1


def test_a_lane_whose_key_is_gone_stops_instead_of_using_another_account(capsys):
    _write(_base(lanes=_items(**{"1": {}, "3": {"key_ref": "friend-a"}})))
    s = _load(3)
    assert _login(s) == {f: "" for f in go.ACCOUNT_FIELDS}
    assert s.account_problem == "Lane 3: key friend-a is not in the vault"
    with pytest.raises(SystemExit) as exc:
        go._load_and_connect()
    assert exc.value.code == 1
    assert "Lane 3: key friend-a is not in the vault" in capsys.readouterr().out


def test_a_missing_vault_proxy_is_a_problem_too():
    _write(_base(lanes=_items(**{"1": {}, "2": {"proxy_ref": "gone"}})))
    s = _load(2)
    assert s.proxy == "" and s.account_problem == "Lane 2: proxy gone is not in the vault"


def test_the_vault_proxy_survives_a_reload_and_never_reaches_the_file():
    _friend_vault()
    _write(_base(proxy_ref="friend-px", lanes=_items(**{"1": {}})))
    s = _load(1)
    assert s.proxy == PROXY
    go._reload_live_config()
    assert s.proxy == PROXY
    go.save_config()
    assert "pw-secret" not in Path(go.CONFIG_PATH).read_text()


def test_a_preset_run_takes_the_login_from_the_vault():
    _friend_vault()
    assert go._active_account({}) == MAIN
    assert go._active_account(_base()) == MAIN
    assert go._active_account({}, "friend-a") == FRIEND


# ── config-set refusals and restarts ────────────────────────────────────

@pytest.fixture
def wide(monkeypatch):
    monkeypatch.setattr(go.console, "width", 200)


@pytest.mark.parametrize(("pair", "message"), [
    ("lane3.key_ref=nobody", "Invalid lane3 override: no key named nobody in the vault"),
    ("lane3.key_ref=friend-px", "Invalid lane3 override: no key named friend-px in the vault"),
    ("lane3.proxy_ref=friend-a", "Invalid lane3 override: no proxy named friend-a in the vault"),
    (f"lane3.proxy={PROXY}", "a proxy with a password goes in the vault"),
    ("lane3.currency=zzz", "currency must be one of"),
])
def test_bad_lane_accounts_are_refused_without_echo(wide, capsys, pair, message):
    _friend_vault()
    _write(_base(lanes=_items(**{"1": {}, "3": {}})))
    before = Path(go.CONFIG_PATH).read_text()
    assert _set([pair]) == 2
    out = capsys.readouterr().out
    assert message in out and "pw-secret" not in out
    assert Path(go.CONFIG_PATH).read_text() == before


@pytest.mark.parametrize("pair", ["access_token=x-secret-1", "lane3.access_token=x-secret-1",
                                  "lane2.cookie=x-secret-1", "lane2.lockdown_token=x-secret-1",
                                  "lane2.token=x-secret-1"])
def test_credentials_never_go_through_config_set(wide, capsys, pair):
    _write(_base(lanes=_items(**{"1": {}, "3": {}})))
    assert _set([pair]) == 2
    out = capsys.readouterr().out
    assert "Refusing to write" in out and "x-secret-1" not in out


def test_only_the_lane_whose_account_changed_restarts(wide, monkeypatch, capsys):
    _friend_vault()
    _write(_base(lanes=_items(**{"1": {}, "2": {}, "3": {}})))
    for lane in (1, 2, 3):
        Path(go.lane_path(go.PID_PATH, lane)).write_text(str(100 + lane))
    monkeypatch.setattr(go.os, "kill", lambda pid, sig: None)
    restarted = []
    monkeypatch.setattr(go, "_restart_lane", lambda lane: restarted.append(lane) or True)
    assert _set(["lane3.key_ref=friend-a", "lane3.currency=trx"]) == 0
    assert restarted == [3]
    assert "Lane 3 restarted: its key, currency or proxy changed." in capsys.readouterr().out
    assert _set(["lane2.proxy=socks5://10.0.0.5:1080"]) == 0
    assert restarted == [3, 2]
    assert _set(["lane2.maxloss=1"]) == 0
    assert restarted == [3, 2]


# ── key verbs ───────────────────────────────────────────────────────────

def test_an_account_is_added_from_three_stdin_lines_and_listed(wide, monkeypatch, capsys):
    _write(_base(lanes=_items(**{"1": {}, "2": {"key_ref": "friend-a"}})))
    _stdin(monkeypatch, *FRIEND.values())
    assert go.cmd_key("put", "friend-a", "Friend A") == 0
    out = capsys.readouterr().out
    assert "Key friend-a added (ends 3210)." in out
    assert all(v not in out for v in FRIEND.values())
    assert _vault().account("friend-a") == FRIEND
    assert go.cmd_key("list") == 0
    out = capsys.readouterr().out
    assert "friend-a" in out and "account" in out and "3210" in out
    assert all(v not in out for v in FRIEND.values())


def test_rotate_replaces_the_login_and_checks_its_kind(monkeypatch, capsys):
    _friend_vault()
    _write(_base())
    new = {**FRIEND, "access_token": "stake-friend-token-new-000000009999"}
    _stdin(monkeypatch, *new.values())
    assert go.cmd_key("rotate", "friend-a") == 0
    out = capsys.readouterr().out
    assert "Key friend-a rotated (ends 9999)." in out and "when they restart" in out
    assert _vault().account("friend-a") == new and _vault().info("friend-a").label == "Friend A"
    _stdin(monkeypatch, "socks5://10.0.0.7:1080")
    assert go.cmd_key("rotate", "friend-px") == 2
    assert "friend-px holds a proxy: rotate it with --kind proxy." in capsys.readouterr().out
    assert go.cmd_key("rotate", "friend-px", None, "proxy") == 0


@pytest.mark.parametrize(("action", "key_id", "kind", "lines", "message"), [
    ("put", "Bad Id", "account", list(FRIEND.values()), "key id must be 1-32 lowercase"),
    ("put", "friend-a", "account", list(FRIEND.values()), "A key named friend-a already exists."),
    ("rotate", "nobody", "account", list(FRIEND.values()), "No key named nobody."),
    ("delete", "default", "account", [], "This key is used by lane 1, 2. Move the lanes to another key first."),
    ("put", "short", "account", ["abc", "x", "y"], "the key must be 8-4096 printable characters"),
    ("put", "tab", "account", [FRIEND["access_token"], "x", "bad\tcookie-secret"], "cookie must be printable text"),
    ("put", "bot", "telegram", ["12345:" + "a" * 35], "The Telegram bot token is stored as telegram"),
    ("put", "px", "proxy", ["ftp://user:pw@host:1"], "a proxy must be an http://"),
])
def test_key_refusals_exit_2_and_never_echo(wide, monkeypatch, capsys, action, key_id, kind, lines,
                                            message):
    _friend_vault()
    _write(_base(key_ref="default", lanes=_items(**{"1": {}, "2": {}})))
    _stdin(monkeypatch, *lines)
    assert go.cmd_key(action, key_id, None, kind) == 2
    out = capsys.readouterr().out
    assert message in out
    assert all(line not in out for line in lines if len(line) > 4)


def test_an_unused_key_is_deleted_and_telegram_has_its_own_note(monkeypatch, capsys):
    _friend_vault()
    _write(_base())
    assert go.cmd_key("delete", "friend-a") == 0
    assert "Key friend-a deleted." in capsys.readouterr().out and _vault().info("friend-a") is None
    _stdin(monkeypatch, "12345:" + "a" * 35)
    assert go.cmd_key("put", "telegram", None, "telegram") == 0
    assert "stakectl tg restart" in capsys.readouterr().out


def test_key_flags_read_the_secret_from_stdin(monkeypatch):
    _write(_base())
    _stdin(monkeypatch, *FRIEND.values())
    monkeypatch.setattr(sys, "argv", ["stake.py", "--key-put", "friend-a", "--key-label", "Friend"])
    with pytest.raises(SystemExit) as exc:
        go.main()
    assert exc.value.code == 0 and _vault().info("friend-a").label == "Friend"


# ── a Cloudflare session per proxy ──────────────────────────────────────

def test_each_proxy_gets_its_own_cookie_cache_and_lock():
    go.state.proxy = ""
    assert go._cf_cache_path() == go.CF_CACHE_PATH and go._cf_proxy() is None
    go.state.proxy = PROXY
    path = go._cf_cache_path()
    assert path != go.CF_CACHE_PATH and "pw-secret" not in path
    assert go._cf_proxy() == ("socks5://10.0.0.9:1080", "friend", "pw-secret")
    go.state.proxy = "socks5://10.0.0.5:1080"
    assert go._cf_cache_path() not in (path, go.CF_CACHE_PATH)


def test_cookies_solved_on_one_ip_are_not_read_on_another(monkeypatch):
    go.state.proxy = ""
    monkeypatch.setattr(go, "_cf_cookie_str", "cf_clearance=host-ip")
    go._cf_cache_save()
    go.state.proxy = PROXY
    go._cf_switch_ip()
    assert go._cf_cookie_str == ""
    go.state.proxy = ""
    go._cf_switch_ip()
    assert go._cf_cookie_str == "cf_clearance=host-ip"


def test_flaresolverr_solves_through_the_lane_proxy(monkeypatch):
    sent = []

    class _Resp:
        def json(self):
            return {"status": "ok", "solution": {"userAgent": "UA", "cookies": [
                {"name": "cf_clearance", "value": "via-proxy"}]}}

    monkeypatch.setattr(go.requests, "post", lambda url, json, timeout: sent.append(json) or _Resp())
    go.state.proxy = PROXY
    assert _REAL_FLARESOLVERR("https://stake.bet") is True
    assert sent[0]["proxy"] == {"url": "socks5://10.0.0.9:1080", "username": "friend",
                                "password": "pw-secret"}
    assert json.loads(Path(go._cf_cache_path()).read_text())["cookie_str"] == "cf_clearance=via-proxy"
    assert not Path(go.CF_CACHE_PATH).exists()
    go.state.proxy = ""
    sent.clear()
    assert _REAL_FLARESOLVERR("https://stake.bet") is True
    assert "proxy" not in sent[0]


def test_nodriver_skips_a_proxy_it_cannot_log_in_to(capsys):
    go.state.proxy = PROXY
    assert _REAL_NODRIVER("https://stake.bet") is False
    assert "nodriver can't log in to a proxy with a password" in capsys.readouterr().out


# ── the Telegram bot token ──────────────────────────────────────────────

def test_the_telegram_bot_reads_its_token_from_the_vault_first(monkeypatch):
    from tg import config as tg_config
    monkeypatch.setenv("STAKE_TG_TOKEN", "12345:env-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert tg_config.bot_token() == "12345:env-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _vault().put("telegram", "67890:" + "b" * 35, "bot", kind="telegram")
    assert tg_config.bot_token() == "67890:" + "b" * 35
