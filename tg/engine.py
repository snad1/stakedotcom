"""BettingEngine — per-user betting instance running in its own thread.

Key features ported from wolfbet TG + Stake CLI:
  - Cross-thread SQLite safety (temp conn for session start, lazy persistent in betting thread)
  - Batched DB writes (flush every 50 bets + save session stats on each flush)
  - _db_save_session(final=False) for periodic saves without setting ended_at
  - Multi-game support via game registry (Limbo + Dice)
  - Cloudflare bypass chain: curl_cffi → cached CF cookies → FlareSolverr
  - Profit-based base bet increment
  - Milestone callbacks
  - Full ISO timestamps with microseconds
"""

import os
import json
import time
import random
import string
import sqlite3
import threading
from datetime import datetime
from collections import deque
from typing import Optional, List

from .config import API_BASES, FLARESOLVERR_URL, CF_CACHE_TTL, MIN_BET, logger
from core.database import init_db, db_connect, cleanup_old_bets
from core.strategy import StrategyRule, load_rules_from_text
from core.engine import compute_next_bet, apply_action, evaluate_rules

# Try curl_cffi for Cloudflare TLS bypass; fall back to requests
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False

import requests

# ── Batching / periodic save constants ──────────────────
BET_BATCH_SIZE     = 50      # flush bets + session stats to DB every N bets
SESSION_SAVE_SECS  = 30      # fallback: save session stats every N seconds
REQUEST_TIMEOUT    = (5, 15) # (connect, read) in seconds
MAX_TIMEOUT_STREAK = 3       # recreate HTTP session after this many consecutive timeouts


def _gen_identifier() -> str:
    chars = string.ascii_letters + string.digits + "_"
    return "".join(random.choices(chars, k=21))


# ── Game registry (same as stake.py) ────────────────────
def _limbo_payload(engine):
    return {
        "multiplierTarget": engine.multiplier_target,
        "identifier": _gen_identifier(),
        "amount": round(engine.current_bet, 8) if engine.current_bet >= MIN_BET else engine.current_bet,
        "currency": engine.currency,
    }


def _limbo_parse(data):
    payout_mult = float(data.get("payoutMultiplier", 0))
    result = float(data.get("resultMultiplier", data.get("result", 0)))
    return {
        "amount": float(data.get("amount", 0)),
        "payout": float(data.get("payout", 0)),
        "payout_mult": payout_mult,
        "result_display": f"{result:.4f}x",
        "result_value": result,
        "is_win": payout_mult > 0,
    }


def _dice_payload(engine):
    return {
        "target": engine.dice_target,
        "condition": engine.dice_condition,
        "identifier": _gen_identifier(),
        "amount": round(engine.current_bet, 8) if engine.current_bet >= MIN_BET else engine.current_bet,
        "currency": engine.currency,
    }


def _dice_parse(data):
    payout_mult = float(data.get("payoutMultiplier", 0))
    result = float(data.get("resultTarget", data.get("result", 0)))
    return {
        "amount": float(data.get("amount", 0)),
        "payout": float(data.get("payout", 0)),
        "payout_mult": payout_mult,
        "result_display": f"{result:.2f}",
        "result_value": result,
        "is_win": payout_mult > 0,
    }


GAMES = {
    "limbo": {
        "label": "Limbo",
        "endpoint": "/limbo/bet",
        "response_key": "limboBet",
        "build_payload": _limbo_payload,
        "parse_result": _limbo_parse,
    },
    "dice": {
        "label": "Dice",
        "endpoint": "/dice/roll",
        "response_key": "diceRoll",
        "build_payload": _dice_payload,
        "parse_result": _dice_parse,
    },
}


def _cfg(config: dict, key: str, default, typ=float):
    """Get config value; treat None as default but allow 0."""
    v = config.get(key)
    return typ(v) if v is not None else typ(default)


class BettingEngine:
    """Self-contained betting engine for one user. Runs in its own thread."""

    def __init__(self, user_id: int, db_path: str, config: dict):
        self.user_id = user_id
        self.db_path = db_path
        self.lock    = threading.Lock()

        # ── auth (use `or` to handle None from presets) ──
        self.access_token   = config.get("access_token") or ""
        self.lockdown_token = config.get("lockdown_token") or ""
        _cookie = config.get("cookie") or ""
        self.cookie         = "" if _cookie.lower() == "none" else _cookie

        # ── game config ──
        self.game              = config.get("game") or "limbo"
        self.currency          = config.get("currency") or "usdt"
        self.multiplier_target = _cfg(config, "multiplier_target", 2.0)
        self.initial_multiplier = self.multiplier_target
        self.dice_target       = _cfg(config, "dice_target", 50.5)
        self.dice_condition    = config.get("dice_condition") or "above"
        self.base_bet          = _cfg(config, "base_bet", 0.0001)
        self.current_bet       = self.base_bet
        self.strategy          = config.get("strategy") or "Martingale"
        self.strategy_key      = config.get("strategy_key") or "2"
        self.win_mult          = _cfg(config, "win_mult", 1.0)
        self.loss_mult         = _cfg(config, "loss_mult", 2.0)
        self.bet_delay         = _cfg(config, "bet_delay", 0)
        self.delay_martin_threshold = _cfg(config, "delay_martin_threshold", 3, int)

        # ── rules ──
        self.custom_rules: List[StrategyRule] = []
        rules_text = config.get("custom_rules_text", "")
        if rules_text:
            self.custom_rules = load_rules_from_text(rules_text)

        # ── stop conditions ──
        self.max_profit      = config.get("max_profit")
        self.max_loss        = config.get("max_loss")
        self.max_bets        = config.get("max_bets")
        self.max_wins        = config.get("max_wins")
        self.stop_on_balance = config.get("stop_on_balance")

        # ── session state ──
        self.running         = False
        self.paused          = False
        self.session_id      = None
        self.session_start   = 0.0
        self.total_bets      = 0
        self.wins            = 0
        self.losses          = 0
        self.profit          = 0.0
        self.wagered         = 0.0
        self.start_balance   = 0.0
        self.current_balance = 0.0
        self.current_streak  = 0
        self.max_win_streak  = 0
        self.max_loss_streak = 0
        self.highest_bet     = 0.0
        self.highest_win     = 0.0
        self.biggest_loss    = 0.0
        self.highest_balance = 0.0
        self.lowest_balance  = float("inf")
        self.bets_per_second = 0.0
        self.bets_per_minute = 0.0
        self.peak_bps        = 0.0
        self.low_bps         = float("inf")
        self.peak_bpm        = 0.0
        self.low_bpm         = float("inf")
        self._bets_this_sec  = 0
        self._bets_this_min  = 0
        self._current_sec    = 0
        self._current_min    = 0
        self.profit_history  = deque(maxlen=40)
        self.profit_history.append(0.0)
        self.recent_bets     = deque(maxlen=5)
        self.status          = "Idle"
        self.last_error      = ""
        self.stop_reason     = ""

        # ── error backoff ──
        self.consecutive_errors = 0
        self._consecutive_timeouts = 0
        self.backoff_delay   = 1.0
        self._insufficient_balance = False
        self._last_api_ms          = 0.0
        self._api_ms_total         = 0.0
        self._api_ms_count         = 0

        # ── strategy internals ──
        self.dalembert_unit  = 0
        self.paroli_count    = 0

        # ── profit-based base bet increment ──
        self.profit_increment     = config.get("profit_increment")
        self.profit_threshold     = config.get("profit_threshold")
        self.next_profit_milestone = float(self.profit_threshold) if self.profit_threshold else 0.0

        # ── milestone config ──
        self.milestone_bets   = _cfg(config, "milestone_bets", 100, int)
        self.milestone_wins   = _cfg(config, "milestone_wins", 0, int)
        self.milestone_losses = _cfg(config, "milestone_losses", 0, int)
        self.milestone_profit = _cfg(config, "milestone_profit", 0)
        self._last_profit_milestone = 0.0

        # ── callbacks ──
        self.on_stop         = None
        self.on_milestone    = None

        # ── batching state ──
        self._bet_queue: list = []
        self._last_session_save = 0.0
        self._db_connection: Optional[sqlite3.Connection] = None

        # ── HTTP session (per-engine, handles CF) ──
        self._http = None
        self._api_base = API_BASES[0]
        self._cf_cookie_str = ""
        self._cf_user_agent = ""

        # ── thread ──
        self._thread: Optional[threading.Thread] = None

    # ── HTTP / Cloudflare ─────────────────────────────────
    def _init_http(self):
        """Create HTTP session. Prefer curl_cffi for TLS fingerprint bypass."""
        if _HAS_CFFI:
            self._http = cffi_requests.Session(impersonate="chrome")
        else:
            self._http = requests.Session()

    def _recreate_http(self):
        """Close and recreate the HTTP session to recover from stale connections."""
        try:
            if self._http:
                self._http.close()
        except Exception:
            pass
        self._init_http()
        logger.info("User %d: HTTP session recreated", self.user_id)

    def _headers(self) -> dict:
        ua = self._cf_user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
        )
        h = {
            "Content-Type":     "application/json",
            "Accept":           "*/*",
            "x-access-token":   self.access_token,
            "x-lockdown-token": self.lockdown_token,
            "x-language":       "en",
            "Origin":           self._api_base.split("/_api")[0],
            "Referer":          self._api_base.split("/_api")[0] + f"/casino/games/{self.game}",
            "User-Agent":       ua,
        }
        cookie_parts = []
        if self._cf_cookie_str:
            cookie_parts.append(self._cf_cookie_str)
        if self.cookie:
            cookie_parts.append(self.cookie)
        if cookie_parts:
            h["Cookie"] = "; ".join(cookie_parts)
        return h

    def _cf_cache_path(self) -> str:
        if self.db_path:
            return os.path.join(os.path.dirname(self.db_path), "cf_cookies.json")
        return os.path.expanduser("~/.stake_cf_cookies.json")

    def _cf_cache_load(self) -> bool:
        try:
            path = self._cf_cache_path()
            if not os.path.exists(path):
                return False
            with open(path) as f:
                cache = json.load(f)
            if time.time() - cache.get("timestamp", 0) > CF_CACHE_TTL:
                return False
            self._cf_cookie_str = cache.get("cookie_str", "")
            self._cf_user_agent = cache.get("user_agent", "")
            return bool(self._cf_cookie_str)
        except Exception:
            return False

    def _cf_cache_save(self):
        try:
            with open(self._cf_cache_path(), "w") as f:
                json.dump({
                    "cookie_str": self._cf_cookie_str,
                    "user_agent": self._cf_user_agent,
                    "timestamp": time.time(),
                }, f)
        except Exception:
            pass

    def _solve_cloudflare(self, site_url: str) -> bool:
        try:
            r = requests.post(FLARESOLVERR_URL, json={
                "cmd": "request.get",
                "url": site_url,
                "maxTimeout": 60000,
            }, timeout=65)
            data = r.json()
            if data.get("status") == "ok":
                sol = data.get("solution", {})
                self._cf_user_agent = sol.get("userAgent", "")
                cookies = sol.get("cookies", [])
                self._cf_cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                if self._cf_cookie_str:
                    self._cf_cache_save()
                return bool(self._cf_cookie_str)
        except Exception:
            pass
        return False

    def _api_post(self, url: str, payload: dict) -> Optional[dict]:
        r = self._http.post(url, headers=self._headers(), json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            body = r.text[:300] if r.text else "(empty)"
            raise ConnectionError(f"HTTP {r.status_code} from {url}: {body}")
        return r.json()

    # ── API ──────────────────────────────────────────────
    def api_get_balances(self) -> list:
        """Fetch all balances via GraphQL."""
        gql_url = self._api_base.split("/_api/casino")[0] + "/_api/graphql"
        query = """query UserBalances {
  user {
    id
    balances {
      available { amount currency __typename }
      __typename
    }
    __typename
  }
}"""
        payload = {"operationName": "UserBalances", "query": query, "variables": {}}
        try:
            r = self._http.post(gql_url, headers=self._headers(), json=payload, timeout=REQUEST_TIMEOUT)
            logger.debug("Balance response %d: %s", r.status_code, r.text[:500])
            if r.status_code == 200:
                data = r.json()
                user = data.get("data", {}).get("user")
                if not user:
                    logger.warning("Balance: no user in response: %s", str(data)[:300])
                    return []
                balances_obj = user.get("balances", {})
                # Stake returns { balances: { available: [{amount, currency}, ...] } }
                available = []
                if isinstance(balances_obj, dict):
                    available = balances_obj.get("available", [])
                elif isinstance(balances_obj, list):
                    # fallback: list of {available: {amount, currency}}
                    for b in balances_obj:
                        av = b.get("available")
                        if av:
                            available.append(av)
                return [{"currency": b.get("currency", ""),
                         "amount": float(b.get("amount", 0))}
                        for b in available if float(b.get("amount", 0)) > 0]
            else:
                logger.warning("Balance HTTP %d: %s", r.status_code, r.text[:300])
        except Exception as e:
            logger.warning("Balance fetch failed: %s", e)
        return []

    def _api_test_connection(self) -> bool:
        """Test auth with a zero-bet. Tries all API bases, with CF bypass chain."""
        game_info = GAMES[self.game]
        build = game_info["build_payload"]
        resp_key = game_info["response_key"]
        endpoint = game_info["endpoint"]

        saved = self.current_bet
        self.current_bet = 0
        payload = build(self)
        self.current_bet = saved

        def _check(data):
            return data and (resp_key in data or
                             (isinstance(data.get("data"), dict) and resp_key in data["data"]))

        def _try_all():
            last_err = None
            for base in API_BASES:
                url = base + endpoint
                try:
                    data = self._api_post(url, payload)
                    if _check(data):
                        return base, None
                    last_err = f"Missing {resp_key} from {url}"
                except Exception as e:
                    last_err = str(e)
            return None, last_err

        # Pass 1: direct
        base, err = _try_all()
        if base:
            self._api_base = base
            return True

        # Pass 2: cached CF cookies
        if "403" in str(err) and self._cf_cache_load():
            base2, err2 = _try_all()
            if base2:
                self._api_base = base2
                return True
            err = err2

        # Pass 3: FlareSolverr
        if "403" in str(err):
            try:
                health = requests.get(
                    FLARESOLVERR_URL.replace("/v1", "/health"), timeout=3)
                fs_ok = health.status_code == 200
            except Exception:
                fs_ok = False
            if fs_ok:
                for domain_base in API_BASES:
                    site = domain_base.split("/_api")[0]
                    if self._solve_cloudflare(site):
                        base3, err3 = _try_all()
                        if base3:
                            self._api_base = base3
                            return True
                        err = err3

        self.last_error = err or "All API domains failed"
        return False

    def _api_place_bet(self, amount: float) -> Optional[dict]:
        """Place a bet. Returns parsed result dict or None."""
        if amount > 0 and amount < MIN_BET:
            amount = MIN_BET

        game_info = GAMES[self.game]
        endpoint = game_info["endpoint"]
        resp_key = game_info["response_key"]
        build = game_info["build_payload"]
        parse = game_info["parse_result"]

        saved = self.current_bet
        self.current_bet = amount
        payload = build(self)
        self.current_bet = saved

        url = self._api_base + endpoint
        try:
            t0 = time.time()
            data = self._api_post(url, payload)
            self._last_api_ms = (time.time() - t0) * 1000
            self._consecutive_timeouts = 0  # success — reset
            if data is None:
                self.last_error = "Empty response"
                return None
            # Check for API errors (e.g. insufficient balance)
            errors = data.get("errors") or data.get("error")
            if errors:
                err_str = str(errors).lower()
                if "insufficient" in err_str or "balance" in err_str:
                    self.last_error = "Insufficient balance"
                    self._insufficient_balance = True
                    return None
                self.last_error = f"API error: {str(errors)[:120]}"
                return None
            self._api_ms_total += self._last_api_ms
            self._api_ms_count += 1
            raw = data.get(resp_key, data)
            if not raw:
                self.last_error = f"Empty {resp_key}"
                return None
            return parse(raw)
        except ConnectionError as e:
            err = str(e)
            if "429" in err:
                self.last_error = "Rate limit 429"
            else:
                self.last_error = err[:120]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            self._consecutive_timeouts += 1
            self.last_error = f"Request timeout (#{self._consecutive_timeouts})"
            if self._consecutive_timeouts >= MAX_TIMEOUT_STREAK:
                logger.warning("User %d: %d consecutive timeouts — recreating HTTP session",
                               self.user_id, self._consecutive_timeouts)
                self._recreate_http()
                self._consecutive_timeouts = 0
        except Exception as e:
            self.last_error = str(e)[:120]
        return None

    # ── DB ───────────────────────────────────────────────
    def _get_conn(self) -> sqlite3.Connection:
        """Return persistent DB connection, creating if needed (in betting thread)."""
        if self._db_connection is None:
            self._db_connection = db_connect(self.db_path)
        return self._db_connection

    def _close_conn(self):
        if self._db_connection:
            try:
                self._db_connection.close()
            except Exception:
                pass
            self._db_connection = None

    def _build_config_snapshot(self) -> str:
        """Build a JSON snapshot of session config for history."""
        snap = {
            "strategy_key": self.strategy_key,
            "loss_mult": self.loss_mult,
            "win_mult": self.win_mult,
            "bet_delay": self.bet_delay,
        }
        if self.strategy_key == "6":  # Delay Martingale
            snap["delay_threshold"] = self.delay_martin_threshold
        if self.game == "dice":
            snap["dice_target"] = self.dice_target
            snap["dice_condition"] = self.dice_condition
        # Stop conditions
        stops = {}
        if self.max_profit is not None: stops["max_profit"] = self.max_profit
        if self.max_loss is not None:   stops["max_loss"] = self.max_loss
        if self.max_bets is not None:   stops["max_bets"] = self.max_bets
        if self.max_wins is not None:   stops["max_wins"] = self.max_wins
        if self.stop_on_balance is not None: stops["min_balance"] = self.stop_on_balance
        if stops:
            snap["stops"] = stops
        # Profit increment
        if self.profit_threshold and self.profit_increment:
            snap["profit_threshold"] = self.profit_threshold
            snap["profit_increment"] = self.profit_increment
        # Rules
        if self.custom_rules:
            snap["rules"] = [r.to_dict() for r in self.custom_rules]
        return json.dumps(snap)

    def _db_start_session(self) -> int:
        """Insert new session row. Uses TEMPORARY connection because start()
        runs in the main thread, but persistent conn must be in betting thread."""
        snapshot = self._build_config_snapshot()
        conn = db_connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO sessions (started_at, currency, game, strategy, base_bet, multiplier,
                                  start_balance, config_snapshot)
            VALUES (?,?,?,?,?,?,?,?)
        """, (datetime.now().isoformat(), self.currency, self.game,
              self.strategy, self.base_bet, self.multiplier_target, self.start_balance,
              snapshot))
        conn.commit()
        sid = c.lastrowid
        conn.close()
        return sid

    def _queue_bet(self, result: dict, profit: float, balance: float):
        """Queue a bet row for batched insertion."""
        self._bet_queue.append((
            self.session_id, datetime.now().isoformat(),
            self.game,
            float(result.get("amount", 0)),
            self.multiplier_target,
            float(result.get("result_value", 0)),
            result.get("result_display", ""),
            "win" if result.get("is_win") else "loss",
            profit, balance,
        ))
        if len(self._bet_queue) >= BET_BATCH_SIZE:
            self._flush_bets()

    def _flush_bets(self):
        """Flush all queued bets to DB and save session stats in one go."""
        if not self._bet_queue:
            return
        try:
            conn = self._get_conn()
            conn.executemany("""
                INSERT INTO bets (session_id, timestamp, game, amount, multiplier_target,
                    result_value, result_display, state, profit, balance_after)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, self._bet_queue)
            conn.commit()
            self._bet_queue.clear()
        except Exception as e:
            logger.error("User %d: DB flush_bets failed: %s", self.user_id, e)
        # always save session stats when flushing bets — keeps both in sync
        self._db_save_session()

    def _db_save_session(self, final: bool = False):
        """Save session stats. If final=True, also set ended_at."""
        try:
            conn = self._get_conn()
            lo_bal = self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance
            fields = (
                self.total_bets, self.wins, self.losses,
                self.profit, self.wagered, self.current_balance,
                self.max_win_streak, self.max_loss_streak,
                self.highest_balance, lo_bal,
                self.highest_win, self.biggest_loss,
                self.bets_per_minute,
                self.bets_per_second,
                self.peak_bps,
                self.low_bps if self.low_bps != float("inf") else 0,
                self.peak_bpm,
                self.low_bpm if self.low_bpm != float("inf") else 0,
                self.session_id,
            )
            if final:
                conn.execute("""
                    UPDATE sessions SET
                        ended_at=?, total_bets=?, wins=?, losses=?,
                        profit=?, wagered=?, end_balance=?,
                        max_win_streak=?, max_loss_streak=?,
                        highest_balance=?, lowest_balance=?,
                        highest_win=?, biggest_loss=?,
                        bets_per_minute=?, bets_per_second=?,
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?
                    WHERE id=?
                """, (datetime.now().isoformat(),) + fields)
            else:
                conn.execute("""
                    UPDATE sessions SET
                        total_bets=?, wins=?, losses=?,
                        profit=?, wagered=?, end_balance=?,
                        max_win_streak=?, max_loss_streak=?,
                        highest_balance=?, lowest_balance=?,
                        highest_win=?, biggest_loss=?,
                        bets_per_minute=?, bets_per_second=?,
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?
                    WHERE id=?
                """, fields)
            conn.commit()
            self._last_session_save = time.time()
        except Exception as e:
            logger.error("User %d: DB save_session failed: %s", self.user_id, e)

    def _periodic_save(self):
        """Flush bets and save session stats periodically."""
        now = time.time()
        if now - self._last_session_save >= SESSION_SAVE_SECS:
            self._flush_bets()
            self._db_save_session()

    # ── STRATEGY (delegates to core.engine) ────────────────
    def _compute_next_bet(self, last_result: str) -> float:
        nxt, self.dalembert_unit, self.paroli_count = compute_next_bet(
            self.strategy_key, self.base_bet, self.current_bet,
            self.loss_mult, self.win_mult, last_result,
            self.current_streak, self.delay_martin_threshold,
            self.dalembert_unit, self.paroli_count,
        )
        return nxt

    def _apply_rules(self, bet_state: str):
        triggered = evaluate_rules(
            self.custom_rules, bet_state, self.wins, self.losses,
            self.total_bets, self.current_streak, self.profit,
            self.current_balance, self.current_bet, self.multiplier_target,
        )
        for rule in triggered:
            mutations = apply_action(
                rule, self.current_bet, self.base_bet,
                self.dice_condition, self.multiplier_target, self.current_streak,
                initial_multiplier=self.initial_multiplier,
            )
            for k, v in mutations.items():
                setattr(self, k, v)

    def _should_stop(self):
        p = self.profit
        b = self.current_balance
        if self.max_profit is not None and p >= self.max_profit:
            return True, f"Profit target: {p:+.8f}"
        if self.max_loss is not None and p <= -abs(self.max_loss):
            return True, f"Max loss: {p:+.8f}"
        if self.max_bets is not None and self.total_bets >= self.max_bets:
            return True, f"Max bets: {self.total_bets}"
        if self.max_wins is not None and self.wins >= self.max_wins:
            return True, f"Max wins: {self.wins}"
        if self.stop_on_balance is not None and b <= self.stop_on_balance:
            return True, f"Balance floor: {b:.8f}"
        return False, ""

    def _check_milestone(self, last_state: str) -> str:
        if self.milestone_bets > 0 and self.total_bets % self.milestone_bets == 0:
            return f"{self.total_bets} bets"
        if self.milestone_wins > 0 and last_state == "win" and self.wins % self.milestone_wins == 0:
            return f"{self.wins} wins"
        if self.milestone_losses > 0 and last_state != "win" and self.losses % self.milestone_losses == 0:
            return f"{self.losses} losses"
        if self.milestone_profit > 0 and self.profit > 0:
            threshold = self.milestone_profit
            current_level = int(self.profit / threshold)
            last_level = int(self._last_profit_milestone / threshold)
            if current_level > last_level:
                self._last_profit_milestone = self.profit
                return f"profit +{self.profit:.8f}"
        return ""

    # ── LIFECYCLE ────────────────────────────────────────
    def start(self) -> bool:
        """Connect, fetch balance, start betting thread. Returns True on success."""
        self._init_http()

        # Test connection (zero-bet) with CF bypass chain
        if not self._api_test_connection():
            return False

        try:
            balances = self.api_get_balances()
        except Exception as e:
            self.last_error = f"Balance fetch failed: {e}"
            return False

        bal = 0.0
        for b in balances:
            if b.get("currency", "").lower() == self.currency.lower():
                bal = float(b.get("amount", 0))
                break
        if bal <= 0:
            self.last_error = f"No {self.currency.upper()} balance found"
            return False

        init_db(self.db_path)
        cleanup_old_bets(self.db_path)
        self.start_balance   = bal
        self.current_balance = bal
        self.highest_balance = bal
        self.lowest_balance  = bal
        self.session_start   = time.time()
        self._last_session_save = time.time()
        self.session_id      = self._db_start_session()
        self.running         = True
        self.status          = "Starting…"

        self._thread = threading.Thread(target=self._betting_loop, daemon=True)
        self._thread.start()
        return True

    # ── RESUME SUPPORT ────────────────────────────────────
    def snapshot_state(self) -> dict:
        """Capture full engine state for resume after restart."""
        rules_ser = json.dumps([r.to_dict() for r in self.custom_rules]) if self.custom_rules else ""
        config = {
            "access_token": self.access_token,
            "lockdown_token": self.lockdown_token,
            "cookie": self.cookie,
            "game": self.game,
            "currency": self.currency,
            "multiplier_target": self.multiplier_target,
            "dice_target": self.dice_target,
            "dice_condition": self.dice_condition,
            "base_bet": self.base_bet,
            "strategy": self.strategy,
            "strategy_key": self.strategy_key,
            "win_mult": self.win_mult,
            "loss_mult": self.loss_mult,
            "bet_delay": self.bet_delay,
            "delay_martin_threshold": self.delay_martin_threshold,
            "custom_rules_text": rules_ser,
            "max_profit": self.max_profit,
            "max_loss": self.max_loss,
            "max_bets": self.max_bets,
            "max_wins": self.max_wins,
            "stop_on_balance": self.stop_on_balance,
            "profit_increment": self.profit_increment,
            "profit_threshold": self.profit_threshold,
            "milestone_bets": self.milestone_bets,
            "milestone_wins": self.milestone_wins,
            "milestone_losses": self.milestone_losses,
            "milestone_profit": self.milestone_profit,
        }
        return {
            "user_id": self.user_id,
            "db_path": self.db_path,
            "config": config,
            "session_id": self.session_id,
            "session_start": self.session_start,
            "initial_multiplier": self.initial_multiplier,
            "current_bet": self.current_bet,
            "total_bets": self.total_bets,
            "wins": self.wins,
            "losses": self.losses,
            "profit": self.profit,
            "wagered": self.wagered,
            "start_balance": self.start_balance,
            "current_balance": self.current_balance,
            "current_streak": self.current_streak,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "highest_bet": self.highest_bet,
            "highest_win": self.highest_win,
            "biggest_loss": self.biggest_loss,
            "highest_balance": self.highest_balance,
            "lowest_balance": self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance,
            "dalembert_unit": self.dalembert_unit,
            "paroli_count": self.paroli_count,
            "next_profit_milestone": self.next_profit_milestone,
            "_last_profit_milestone": self._last_profit_milestone,
        }

    def restore_state(self, snap: dict):
        """Restore runtime state from a snapshot dict."""
        self.session_id        = snap["session_id"]
        self.session_start     = snap["session_start"]
        self.initial_multiplier = snap.get("initial_multiplier", self.multiplier_target)
        self.current_bet       = snap["current_bet"]
        self.total_bets        = snap["total_bets"]
        self.wins              = snap["wins"]
        self.losses            = snap["losses"]
        self.profit            = snap["profit"]
        self.wagered           = snap["wagered"]
        self.start_balance     = snap["start_balance"]
        self.current_balance   = snap["current_balance"]
        self.current_streak    = snap["current_streak"]
        self.max_win_streak    = snap["max_win_streak"]
        self.max_loss_streak   = snap["max_loss_streak"]
        self.highest_bet       = snap["highest_bet"]
        self.highest_win       = snap["highest_win"]
        self.biggest_loss      = snap["biggest_loss"]
        self.highest_balance   = snap["highest_balance"]
        self.lowest_balance    = snap.get("lowest_balance", float("inf"))
        self.dalembert_unit    = snap.get("dalembert_unit", 0)
        self.paroli_count      = snap.get("paroli_count", 0)
        self.next_profit_milestone  = snap.get("next_profit_milestone", 0.0)
        self._last_profit_milestone = snap.get("_last_profit_milestone", 0.0)

    def start_resumed(self) -> bool:
        """Resume a saved session. Re-tests connection (with CF bypass) but skips balance fetch / new DB session."""
        self._init_http()
        init_db(self.db_path)

        # Must pass the same CF bypass chain as start() so bets don't 403
        if not self._api_test_connection():
            self.last_error = f"Resume connection failed: {self.last_error}"
            return False

        self.running = True
        self.paused = False
        self.status = "Resumed"
        self._last_session_save = time.time()
        # Recalculate speed from restored totals so status isn't 0 after resume
        elapsed = time.time() - self.session_start if self.session_start else 0
        if elapsed > 0 and self.total_bets > 0:
            self.bets_per_second = self.total_bets / elapsed
            self.bets_per_minute = self.bets_per_second * 60
        self._thread = threading.Thread(target=self._betting_loop, daemon=True)
        self._thread.start()
        logger.info("User %d: Resumed session #%d", self.user_id, self.session_id)
        return True

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def get_status(self) -> dict:
        elapsed = time.time() - self.session_start if self.session_start else 0
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        wr = f"{self.wins/self.total_bets*100:.1f}%" if self.total_bets > 0 else "—"
        return {
            "running": self.running,
            "paused": self.paused,
            "session_id": self.session_id,
            "uptime": f"{h}h {m}m {s}s",
            "bets": self.total_bets,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": wr,
            "profit": self.profit,
            "wagered": self.wagered,
            "balance": self.current_balance,
            "start_balance": self.start_balance,
            "current_bet": self.current_bet,
            "highest_bet": self.highest_bet,
            "streak": self.current_streak,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "highest_balance": self.highest_balance,
            "lowest_balance": self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance,
            "highest_win": self.highest_win,
            "biggest_loss": self.biggest_loss,
            "bps": self.bets_per_second,
            "bpm": self.bets_per_minute,
            "base_bet": self.base_bet,
            "strategy": self.strategy,
            "game": self.game,
            "multiplier": self.multiplier_target,
            "currency": self.currency.upper(),
            "status": self.status,
            "last_error": self.last_error,
            "api_ms": self._last_api_ms,
            "api_avg_ms": (self._api_ms_total / self._api_ms_count) if self._api_ms_count > 0 else 0,
        }

    # ── BETTING LOOP ─────────────────────────────────────
    def _betting_loop(self):
        last_result = "none"
        logger.info("User %d: Session #%d started (%s %s)",
                     self.user_id, self.session_id, self.game, self.strategy)

        while self.running:
            if self.paused:
                self.status = "PAUSED"
                time.sleep(0.5)
                continue

            # stop conditions
            stop, reason = self._should_stop()
            if stop:
                self.running = False
                self.stop_reason = reason
                self.status = f"STOPPED: {reason}"
                self._flush_bets()
                self._db_save_session(final=True)
                break

            # bet pacing
            if self.bet_delay > 0:
                time.sleep(self.bet_delay)

            # place bet
            self.status = f"Placing bet #{self.total_bets + 1}…"
            result = self._api_place_bet(self.current_bet)

            if result is None:
                if self._insufficient_balance:
                    self.running = False
                    self.stop_reason = f"Insufficient balance for bet {self.current_bet:.8f}"
                    self.status = f"STOPPED: {self.stop_reason}"
                    self._flush_bets()
                    self._db_save_session(final=True)
                    break
                self.consecutive_errors += 1
                self.backoff_delay = min(self.backoff_delay * 2, 30.0)
                time.sleep(self.backoff_delay)
                continue

            is_win       = result["is_win"]
            amount_used  = result["amount"]
            payout       = result["payout"]
            raw_profit   = payout - amount_used if is_win else -amount_used
            bet_state    = "win" if is_win else "loss"
            rd           = result["result_display"]

            with self.lock:
                self.consecutive_errors = 0
                self.backoff_delay = 1.0
                self.total_bets += 1
                self.wagered    += amount_used
                self.profit     += raw_profit
                # Balance: add payout on win, subtract amount on loss
                self.current_balance += raw_profit

                if is_win:
                    self.wins += 1
                    if raw_profit > self.highest_win:
                        self.highest_win = raw_profit
                    self.current_streak = max(self.current_streak, 0) + 1
                    self.max_win_streak = max(self.max_win_streak, self.current_streak)
                else:
                    self.losses += 1
                    if amount_used > self.biggest_loss:
                        self.biggest_loss = amount_used
                    self.current_streak = min(self.current_streak, 0) - 1
                    self.max_loss_streak = max(self.max_loss_streak, abs(self.current_streak))

                if amount_used > self.highest_bet:
                    self.highest_bet = amount_used
                if self.current_balance > self.highest_balance:
                    self.highest_balance = self.current_balance
                if self.current_balance < self.lowest_balance:
                    self.lowest_balance = self.current_balance

                if self.total_bets % 5 == 0:
                    self.profit_history.append(self.profit)

                self.recent_bets.append({
                    "n": self.total_bets, "time": datetime.now().isoformat(),
                    "amt": amount_used, "result": rd,
                    "state": bet_state, "pnl": raw_profit, "bal": self.current_balance,
                })

                # BPS/BPM tracking
                now = time.time()
                elapsed = now - self.session_start
                if elapsed > 0:
                    self.bets_per_second = self.total_bets / elapsed
                    self.bets_per_minute = self._bets_this_min

                sec_key = int(now)
                min_key = int(now) // 60
                if sec_key != self._current_sec:
                    if self._current_sec > 0 and self._bets_this_sec > 0:
                        if self._bets_this_sec > self.peak_bps:
                            self.peak_bps = self._bets_this_sec
                        if self._bets_this_sec < self.low_bps:
                            self.low_bps = self._bets_this_sec
                    self._current_sec = sec_key
                    self._bets_this_sec = 1
                else:
                    self._bets_this_sec += 1
                if min_key != self._current_min:
                    if self._current_min > 0 and self._bets_this_min > 0:
                        if self._bets_this_min > self.peak_bpm:
                            self.peak_bpm = self._bets_this_min
                        if self._bets_this_min < self.low_bpm:
                            self.low_bpm = self._bets_this_min
                    self._current_min = min_key
                    self._bets_this_min = 1
                else:
                    self._bets_this_min += 1

                sign  = "+" if raw_profit >= 0 else ""
                emoji = "W" if is_win else "L"
                self.status = f"{emoji} {'WIN' if is_win else 'LOSS'} | {rd} | P/L: {sign}{raw_profit:.8f}"
                self.last_error = ""

            # queue bet for batched DB write
            self._queue_bet(result, raw_profit, self.current_balance)

            # periodic session save
            self._periodic_save()

            # profit-based base bet increment
            if (self.profit_increment and self.profit_threshold
                    and self.profit >= self.next_profit_milestone):
                self.base_bet += self.profit_increment
                self.next_profit_milestone += self.profit_threshold
                logger.info("User %d: PROFIT INCREMENT base_bet → %.8f (next at %.8f profit)",
                            self.user_id, self.base_bet, self.next_profit_milestone)

            # rule-based strategy
            if self.strategy_key == "7" and self.custom_rules:
                self._apply_rules(bet_state)

            raw_next = self._compute_next_bet(bet_state)
            self.current_bet = max(self.base_bet, raw_next)
            last_result = bet_state

            # milestone callbacks
            if self.on_milestone:
                reason = self._check_milestone(bet_state)
                if reason:
                    try:
                        s = self.get_status()
                        s["milestone_reason"] = reason
                        self.on_milestone(s)
                        logger.info("User %d: Milestone fired: %s", self.user_id, reason)
                    except Exception as e:
                        logger.error("User %d: Milestone callback failed: %s", self.user_id, e)

        # session ended — flush remaining bets and save final state
        self._flush_bets()
        if self.session_id:
            self._db_save_session(final=True)
        self._close_conn()
        logger.info("User %d: Session #%d ended — %s",
                     self.user_id, self.session_id, self.stop_reason or "stopped")
        if self.on_stop:
            try:
                self.on_stop(self.stop_reason or "Stopped by user")
            except Exception:
                pass
