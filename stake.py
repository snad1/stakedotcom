#!/usr/bin/env python3
"""
+----------------------------------------------------------+
|   Stake AutoBot -- Multi-Game Auto-Betting Engine        |
|   Limbo + Dice | Live TUI | Session logging & strategies |
+----------------------------------------------------------+
"""

import os, sys, json, time, sqlite3, threading, signal, random, string, argparse, logging, re, math, queue, csv
import fcntl
import hashlib
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, List
from logging.handlers import TimedRotatingFileHandler
from shared.core.session_rules import (
    RECURRING_DELAY_MAX, clamp, parse_streak_delay, record_outcome,
    restart_delay_for, should_restart, streak_delay_for,
    streak_reset_stop_reason, streak_reset_triggered, streakbet_loss_factor,
)
from shared.core.stats import fmt_duration, sessions_time_span, stats_date_window
from shared.core.database import ensure_sessions_lane, cleanup_old_bets, cleanup_live_bets
from shared.core.diagnostics import percentile, benchmark_verdict
from shared.core.net import validate_proxy, mask_proxy
from shared.core.validation import finite_float
# The lane registry, its writers and the account vault came with casino-shared 0.12.0; an
# older install lacks them, so say how to fix it instead of a traceback.
try:
    from shared.core.lanes import (
        LANES_MAX, LANE_SPLIT_HELP, LANE_UNSET_VALUES,
        lane_path, lanes_count, lane_split, lane_config,
        lane_overrides, split_budget_on, split_base_bet,
        check_lane_keys, lane_share, lane_equity, lane_budget_exceeded, recommend_lanes,
        LANE_ID_MAX, LANE_NAME_HELP, LANE_NAME_RE, default_lane_name, lane_file_ids, lane_ids,
        lane_name, lane_registry, lanes_max, next_lane_id, registry_block,
        set_enabled_count, split_registry_pairs, account_lanes, lane_account,
        request_drain, take_drain,
    )
    from shared.core.vault import (
        KEY_ID_RE, Vault, VaultError, check_account, check_secret, vault_account, vault_info,
        vault_secret,
    )
except ImportError:
    print("casino-shared 0.12.0 or newer is needed: run stakectl update", file=sys.stderr)
    sys.exit(1)
from shared.core.run_increment import (
    PROFIT_INCREMENT_SCOPES, check_profit_increment, db_max_session_id,
    increment_on, load_run, next_milestone, parse_scope, resume_base_bet,
    run_is_stale, run_path, run_profit_done, save_run, bumps_for_base, rebaseline,
)
try:
    import readline
except ImportError:
    pass

try:
    import requests
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich.align import Align
    from rich.rule import Rule
    from rich.markup import escape
    from rich import box
except ImportError:
    print("Missing dependencies. Run:  pip install requests rich")
    sys.exit(1)

# curl_cffi impersonates Chrome TLS fingerprint to bypass Cloudflare
_HAS_CFFI = False
try:
    from curl_cffi import requests as cffi_requests
    _http = cffi_requests.Session(impersonate="chrome")
    _HAS_CFFI = True
except ImportError:
    try:
        import cloudscraper
        _http = cloudscraper.create_scraper()
    except ImportError:
        _http = requests.Session()

_warned_no_cffi_proxy = False

def _recreate_http(impersonate: str = "chrome"):
    """Replace the global HTTP session with a new curl_cffi session using the given profile.

    v1.12.0: also the single place a saved / CLI proxy reaches the client.
    Without curl_cffi the fallback session is kept and a proxy is a no-op:
    say so once instead of silently going direct.
    """
    global _http, _warned_no_cffi_proxy
    if not _HAS_CFFI:
        if getattr(state, "proxy", None) and not _warned_no_cffi_proxy:
            _warned_no_cffi_proxy = True
            logger.warning("proxy set but curl_cffi is not installed -- proxy is ignored "
                           "(pip install curl_cffi)")
        return
    try:
        _http.close()
    except Exception:
        pass
    kwargs = {"impersonate": impersonate}
    if getattr(state, "proxy", None):
        kwargs["proxy"] = state.proxy
    _http = cffi_requests.Session(**kwargs)

# FlareSolverr: solve Cloudflare challenges, then use cookies with curl_cffi
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
CF_CACHE_PATH = os.path.expanduser("~/.stake_cf_cookies.json")
CF_CACHE_TTL  = 1800  # 30 minutes -- cf_clearance typically lasts ~30min
_cf_cookie_str = ""   # Cloudflare cookies from FlareSolverr
_cf_user_agent = ""   # User-Agent that solved the challenge

def _cf_cache_path() -> str:
    """The cookie cache of the IP this lane reaches Stake from: cf_clearance is bound to the IP,
    so lanes on one proxy (or on none) share a cache and lanes on another proxy get their own."""
    proxy = getattr(state, "proxy", "") or ""
    if not proxy:
        return CF_CACHE_PATH
    root, ext = os.path.splitext(CF_CACHE_PATH)
    return f"{root}.{hashlib.sha256(proxy.encode()).hexdigest()[:12]}{ext}"


def _cf_proxy():
    """(server, username, password) of this lane's proxy for a CF solver, or None for the host IP."""
    proxy = getattr(state, "proxy", "") or ""
    if not proxy:
        return None
    u = urlsplit(proxy)
    server = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    return server, u.username or "", u.password or ""


def _cf_cache_load() -> bool:
    """Load cached CF cookies if still valid."""
    global _cf_cookie_str, _cf_user_agent
    path = _cf_cache_path()
    try:
        if not os.path.exists(path):
            return False
        with open(path) as f:
            cache = json.load(f)
        age = time.time() - cache.get("timestamp", 0)
        if age > CF_CACHE_TTL:
            return False
        _cf_cookie_str = cache.get("cookie_str", "")
        _cf_user_agent = cache.get("user_agent", "")
        return bool(_cf_cookie_str)
    except Exception:
        return False

def _cf_switch_ip() -> None:
    """The lane now reaches Stake from another IP: drop the old IP's CF cookies, take the new one's."""
    global _cf_cookie_str, _cf_user_agent
    _cf_cookie_str = _cf_user_agent = ""
    _cf_cache_load()


def _cf_cache_save():
    """Save CF cookies to disk for reuse across restarts and lanes.

    v1.13.0: written atomically (tmp + os.replace) because parallel lanes
    read this file while another lane may be writing it.
    """
    path = _cf_cache_path()
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({
                "cookie_str": _cf_cookie_str,
                "user_agent": _cf_user_agent,
                "timestamp": time.time(),
            }, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass

CF_LOCK_TIMEOUT = 180.0


def _chrome_args() -> list[str]:
    """Chromium flags; the sandbox stays on unless the bot runs as root."""
    args = ["--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--headless=new",
            "--disable-features=IsolateOrigins,site-per-process"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args[:0] = ["--no-sandbox", "--disable-setuid-sandbox"]
    return args


class _cf_reauth_lock:
    """v1.13.0 -- cross-lane (cross-process) lock around Cloudflare re-auth:
    an fcntl lock file next to this lane's CF cache, so only one lane per IP runs the CF
    solvers at a time; a lane that waited then finds the fresh cookies."""
    def __enter__(self):
        self._fd = os.open(f"{_cf_cache_path()}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + CF_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    raise ConnectionError(
                        "another process is still solving Cloudflare; try again shortly")
                time.sleep(0.25)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
        return False

def _flaresolverr_available() -> bool:
    try:
        r = requests.get(FLARESOLVERR_URL.replace("/v1", "/health"), timeout=3)
        return r.status_code == 200
    except Exception:
        return False

def _solve_cloudflare(site_url: str) -> bool:
    """Use FlareSolverr/Byparr to solve Cloudflare challenge. Caches cookies to disk."""
    global _cf_cookie_str, _cf_user_agent
    try:
        req = {"cmd": "request.get", "url": site_url, "maxTimeout": 60000}
        proxy = _cf_proxy()
        if proxy:
            req["proxy"] = {"url": proxy[0], **({"username": proxy[1], "password": proxy[2]}
                                                 if proxy[1] else {})}
        r = requests.post(FLARESOLVERR_URL, json=req, timeout=65)
        data = r.json()
        if data.get("status") == "ok":
            sol = data.get("solution", {})
            _cf_user_agent = sol.get("userAgent", "")
            cookies = sol.get("cookies", [])
            has_clearance = any(c.get("name") == "cf_clearance" for c in cookies)
            if has_clearance:
                _cf_cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                _cf_cache_save()
                return True
    except Exception:
        pass
    return False

def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False

_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
    get: () => [{name: 'Chrome PDF Plugin'}, {name: 'Chrome PDF Viewer'}, {name: 'Native Client'}]
});
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""

def _solve_cf_playwright(site_url: str) -> bool:
    """Use patchright (preferred) or Playwright with new headless mode + Turnstile click."""
    global _cf_cookie_str, _cf_user_agent
    sync_playwright = None
    backend = None
    try:
        from patchright.sync_api import sync_playwright as _spw
        sync_playwright = _spw
        backend = "patchright"
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright as _spw
            sync_playwright = _spw
            backend = "playwright"
        except ImportError as e:
            console.print(f"  [red]Playwright not installed: {e}[/]")
            return False
    console.print(f"  [dim]Using {backend} backend[/]")

    try:
        with sync_playwright() as pw:
            try:
                proxy = _cf_proxy()
                browser = pw.chromium.launch(
                    headless=True,
                    args=_chrome_args(),
                    **({"proxy": {"server": proxy[0], **({"username": proxy[1],
                                                         "password": proxy[2]}
                                                        if proxy[1] else {})}}
                       if proxy else {}),
                )
            except Exception as e:
                console.print(f"  [red]Chromium launch failed: {e}[/]")
                console.print("  [yellow]Run: playwright install chromium && playwright install-deps chromium[/]")
                return False

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            context.add_init_script(_STEALTH_INIT_JS)
            page = context.new_page()

            try:
                page.goto(site_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                console.print(f"  [red]Page navigation failed: {e}[/]")
                browser.close()
                return False

            def _check_cookies():
                cookies = context.cookies()
                cf = next((c for c in cookies if c["name"] == "cf_clearance"), None)
                return cf, cookies

            # Phase 1: passive wait (15s) -- many CF challenges auto-resolve
            for _ in range(15):
                cf, cookies = _check_cookies()
                if cf:
                    break
                page.wait_for_timeout(1000)

            # Phase 2: try clicking Turnstile checkbox if visible
            cf, cookies = _check_cookies()
            if not cf:
                try:
                    iframes = page.frames
                    for frame in iframes:
                        if "challenges.cloudflare" in (frame.url or "") or "turnstile" in (frame.url or "").lower():
                            try:
                                checkbox = frame.locator("input[type=checkbox]")
                                if checkbox.count() > 0:
                                    checkbox.first.click(timeout=5000)
                                    console.print("  [dim]Clicked Turnstile checkbox[/]")
                                    break
                            except Exception:
                                pass
                except Exception:
                    pass

            # Phase 3: extended wait (30s more)
            for i in range(30):
                cf, cookies = _check_cookies()
                if cf:
                    _cf_cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    try:
                        _cf_user_agent = page.evaluate("navigator.userAgent")
                    except Exception:
                        pass
                    _cf_cache_save()
                    console.print(f"  [green]Got cf_clearance via Playwright[/]")
                    browser.close()
                    return True
                page.wait_for_timeout(1000)

            # Diagnostic: what's the page actually showing?
            try:
                title = page.title()
                body = page.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''")
                console.print(f"  [yellow]Playwright timed out. Page title: {title!r}[/]")
                if body:
                    console.print(f"  [dim]Body preview: {body[:200]}...[/]")
            except Exception:
                pass
            browser.close()
            return False
    except Exception as e:
        console.print(f"  [red]Playwright error: {type(e).__name__}: {e}[/]")
        return False


def _find_chrome_binary() -> Optional[str]:
    """Locate any Chrome/Chromium binary: system Chrome → Playwright API → cache search."""
    for p in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
              "/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/snap/bin/chromium"):
        if os.path.exists(p):
            return p
    # Ask Playwright directly for its bundled Chromium path
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
            if path and os.path.exists(path):
                return path
    except Exception:
        pass
    # Last-resort: scan cache directories
    cache_roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
        os.path.expanduser("~/.cache/ms-playwright"),
        "/root/.cache/ms-playwright",
        "/usr/local/share/ms-playwright",
    ]
    for root in cache_roots:
        if not root or not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root), reverse=True):
            if "chromium" in entry.lower():
                for sub in ("chrome-linux/chrome", "chrome-linux/headless_shell",
                            "chrome-linux64/chrome", "chrome/chrome"):
                    p = os.path.join(root, entry, sub)
                    if os.path.exists(p):
                        return p
    return None

_NODRIVER_PATCHED = False

def _patch_nodriver_cdp():
    """Patch nodriver to tolerate newer Chrome CDP schema (missing sameParty etc.)."""
    global _NODRIVER_PATCHED
    if _NODRIVER_PATCHED:
        return
    try:
        from nodriver.cdp import network as _net
        _orig = _net.Cookie.from_json.__func__

        @classmethod
        def _safe_from_json(cls, json):
            j = dict(json)
            j.setdefault("sameParty", False)
            j.setdefault("sameSite", None)
            j.setdefault("priority", "Medium")
            j.setdefault("sourceScheme", "Unset")
            j.setdefault("sourcePort", 0)
            return _orig(cls, j)

        _net.Cookie.from_json = _safe_from_json
        _NODRIVER_PATCHED = True
    except Exception:
        pass

def _solve_cf_nodriver(site_url: str) -> bool:
    """Use nodriver (undetected-chromedriver successor) -- last-resort CF bypass."""
    global _cf_cookie_str, _cf_user_agent
    proxy = _cf_proxy()
    if proxy and proxy[1]:
        console.print("  [dim]nodriver can't log in to a proxy with a password (skip).[/]")
        logger.info("nodriver skipped: the lane's proxy needs a login nodriver can't send")
        return False
    try:
        import asyncio
        import nodriver as uc
    except ImportError:
        return False

    _patch_nodriver_cdp()

    chrome_path = _find_chrome_binary()
    if not chrome_path:
        console.print("  [red]nodriver: no Chrome/Chromium binary found.[/]")
        console.print("  [yellow]Install: apt install -y chromium  OR  install google-chrome-stable[/]")
        return False

    async def _run():
        global _cf_cookie_str, _cf_user_agent
        browser = None
        try:
            browser = await asyncio.wait_for(
                uc.start(
                    headless=True,
                    browser_executable_path=chrome_path,
                    browser_args=_chrome_args() + (
                        [f"--proxy-server={proxy[0]}"] if proxy else []),
                ),
                timeout=20,
            )
            page = await asyncio.wait_for(browser.get(site_url), timeout=30)
            for i in range(45):
                try:
                    cookies = await asyncio.wait_for(browser.cookies.get_all(), timeout=5)
                except asyncio.TimeoutError:
                    cookies = []
                cf = next((c for c in cookies if c.name == "cf_clearance"), None)
                if cf:
                    _cf_cookie_str = "; ".join(f"{c.name}={c.value}" for c in cookies)
                    try:
                        _cf_user_agent = await page.evaluate("navigator.userAgent")
                    except Exception:
                        pass
                    _cf_cache_save()
                    console.print(f"  [green]Got cf_clearance via nodriver after {i+1}s[/]")
                    return True
                await asyncio.sleep(1)
            console.print("  [yellow]nodriver: timed out waiting for cf_clearance[/]")
            return False
        finally:
            if browser:
                try:
                    browser.stop()
                except Exception:
                    pass

    try:
        return asyncio.run(asyncio.wait_for(_run(), timeout=120))
    except asyncio.TimeoutError:
        console.print("  [red]nodriver: hard 120s timeout -- process was hanging[/]")
        return False
    except Exception as e:
        console.print(f"  [red]nodriver error: {type(e).__name__}: {e}[/]")
        return False

# ===========================================================
#  CONSTANTS
# ===========================================================
# stake.bet uses REST API and has lighter Cloudflare than stake.com
# Override with STAKE_API_BASE env var or --api-base flag (useful when proxying
# through a Cloudflare Worker -- then API_BASES becomes [worker_url + "/_api/casino"]).
_API_BASE_OVERRIDE = os.environ.get("STAKE_API_BASE", "").rstrip("/")
if _API_BASE_OVERRIDE:
    API_BASES = [_API_BASE_OVERRIDE + "/_api/casino"]
else:
    API_BASES = ["https://stake.bet/_api/casino", "https://stake.com/_api/casino"]
API_BASE     = API_BASES[0]  # auto-detected during connection test
DB_PATH      = os.path.expanduser("~/.stake_autobot.db")
CONFIG_PATH  = os.path.expanduser("~/.stake_autobot.json")
STATE_PATH   = os.path.expanduser("~/.stake_autobot_live.json")
PID_PATH     = os.path.expanduser("~/.stake_autobot.pid")
# v1.15.0 -- run-wide profit increment tracking (one file per lane)
RUN_PATH     = os.environ.get("STAKE_RUN_PATH", os.path.expanduser("~/.stake_run.json"))
PRESET_PATH  = os.path.expanduser("~/.stake_presets.json")
# A lane's restart or removal request for its next session end (.laneK for lanes 2+).
DRAIN_PATH   = os.environ.get("STAKE_DRAIN_PATH", os.path.expanduser("~/.stake_autobot.drain"))
STAKECTL     = os.environ.get("STAKE_STAKECTL", os.path.expanduser("~/.local/bin/stakectl"))
LOG_DIR      = os.path.expanduser("~/.stake_logs")
VERSION      = "1.20.0"
MIN_BET      = 0.0001   # Stake.com minimum bet
APP_ENV      = os.environ.get("APP_ENV", "production")

# -- Daily rotating logger --
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("stake")
logger.setLevel(logging.WARNING)
_log_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, "stake.log"),
    when="midnight", backupCount=30, utc=False,
)
_log_handler.suffix = "%Y-%m-%d"
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_log_handler)

# ===========================================================
#  PARALLEL LANES  (v1.13.0, wolfbet v2.35.0 / v2.36.0)
# ===========================================================
# A lane is one bot process with its own session, chain, counters, stops and
# pauses. Lane 1 keeps the classic paths; lanes 2..N run side by side on the
# same account, config file, CF cookie cache and stats DB (`--lane K`).
# Helpers: shared.core.lanes.
_BASE_STATE_PATH, _BASE_PID_PATH = STATE_PATH, PID_PATH

# Every setting: how a change reaches running lanes, and whether one lane may override it.
#   live    - a running lane re-reads it on SIGHUP
#   lane    - the lane picks it up when it restarts
#   service - shared by every lane, read at start: every lane must restart
# The login itself, the game and the storage settings are shared; a lane may bet from its own
# account through key_ref, currency and proxy / proxy_ref. The Cloudflare worker / WARP /
# solver settings are CLI flags and env vars (--api-base, STAKE_API_BASE, FLARESOLVERR_URL),
# not config keys, so they are shared by construction.
_LIVE_PER_LANE = (
    "base_bet", "basebet_pct", "bet_delay", "custom_rules_text",
    "max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold", "recurring", "recurring_delay",
    "streak_reset_losses", "streak_reset_delay",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "proxy",
)
_LANE_RESTART = (
    "currency", "multiplier_target", "dice_target", "dice_condition",
    "strategy", "strategy_key", "win_mult", "loss_mult", "delay_martin_threshold",
    "profit_increment_scope", "key_ref", "proxy_ref",
)
_SETTINGS = {
    **{k: ("live", True) for k in _LIVE_PER_LANE},
    **{k: ("live", False) for k in ("purge_days", "db_write_mode")},
    **{k: ("lane", True) for k in _LANE_RESTART},
    **{k: ("service", False) for k in ("access_token", "lockdown_token", "cookie", "game")},
}
_LIVE_RELOAD_KEYS = frozenset(k for k, (apply, _) in _SETTINGS.items() if apply == "live")
_LANE_FORBIDDEN_KEYS = frozenset(k for k, (_, per_lane) in _SETTINGS.items() if not per_lane)
# Never listed as waiting for a restart: the login.
_PENDING_HIDDEN = frozenset({"access_token", "lockdown_token", "cookie"})


def _restart_only(keys) -> list:
    """The keys among `keys` that a running lane picks up only when it restarts."""
    return sorted(k for k in set(keys) if k in _SETTINGS and k not in _LIVE_RELOAD_KEYS)

# The host key vault: stake's main login is the account `default` (its three secrets, one
# entry), the Telegram bot token `telegram`. A lane bets from another account with key_ref.
VAULT_SERVICE = "stake"
VAULT_KEY_ID = "default"
TG_KEY_ID = "telegram"
ACCOUNT_FIELDS = ("access_token", "lockdown_token", "cookie")

_log_tag = ""


def _set_lane_log_prefix(lane: int, name: str) -> None:
    """`[L3]`, or `[L3 friend-A]` once the lane has its own name; lane 1 has none until named.
    The formatter is replaced only when the tag changes (a reload usually changes nothing)."""
    global _log_tag
    named = name != default_lane_name(lane)
    tag = "" if lane <= 1 and not named else f"[L{lane} {name}] " if named else f"[L{lane}] "
    if tag == _log_tag:
        return
    _log_tag = tag
    _log_handler.setFormatter(logging.Formatter(
        f"%(asctime)s  %(levelname)-7s  {tag}%(message)s", datefmt="%Y-%m-%d %H:%M:%S"))


def _apply_lane(lane) -> None:
    """Switch this process to lane `lane`: state/pid paths and the log prefix.
    Raises ValueError outside 1..LANE_ID_MAX."""
    global STATE_PATH, PID_PATH
    lane = 1 if lane is None else int(lane)
    if not 1 <= lane <= LANE_ID_MAX:
        raise ValueError(f"--lane must be 1-{LANE_ID_MAX}, got {lane}")
    state.lane = lane
    if lane > 1:
        STATE_PATH = lane_path(_BASE_STATE_PATH, lane)
        PID_PATH = lane_path(_BASE_PID_PATH, lane)
    _set_lane_log_prefix(lane, lane_name(_read_config_raw() or {}, lane))

CURRENCIES = ["btc", "eth", "ltc", "doge", "trx", "bch", "xrp", "usdt", "bnb", "ada", "matic"]

STRATEGIES = {
    "1": ("Flat Bet",          "Same amount every single bet -- low risk, low variance"),
    "2": ("Martingale",        "Double bet on loss, reset to base on win -- classic high-risk"),
    "3": ("Anti-Martingale",   "Double bet on win, reset to base on loss -- ride hot streaks"),
    "4": ("D'Alembert",        "Add 1 unit on loss, remove 1 unit on win -- gentler progression"),
    "5": ("Paroli (3-step)",   "Double on win up to 3x then reset -- safer streak play"),
    "6": ("Delay Martingale",  "Flat for N losses then double -- delays escalation"),
    "7": ("Rule-Based",        "Build custom conditions & actions -- full control"),
}

# ===========================================================
#  GAME REGISTRY  (REST API)
# ===========================================================
# Each game defines:
#   endpoint:     REST path appended to API_BASE (e.g. "/limbo/bet")
#   response_key: top-level key in JSON response
#   build_payload(state) -> dict for POST body
#   parse_result(data) -> dict with keys:
#       amount, payout, payout_mult, result_display, is_win, raw_state

GAMES = {}

def _register_game(name, label, endpoint, response_key, build_payload, parse_result):
    GAMES[name] = {
        "label": label,
        "endpoint": endpoint,
        "response_key": response_key,
        "build_payload": build_payload,
        "parse_result": parse_result,
    }

def _gen_identifier() -> str:
    chars = string.ascii_letters + string.digits + "_"
    return "".join(random.choices(chars, k=21))


def _nested_result(data) -> float:
    """Fallback for replies that carry the roll only under ``state.result``."""
    st = data.get("state")
    return st.get("result", 0) if isinstance(st, dict) else 0


# -- LIMBO --
def _limbo_payload(st):
    return {
        "multiplierTarget": st.multiplier_target,
        "identifier": _gen_identifier(),
        "amount": round(st.current_bet, 8) if st.current_bet >= MIN_BET else st.current_bet,
        "currency": st.currency,
    }

def _limbo_parse(data):
    payout_mult = float(data.get("payoutMultiplier", 0))
    result = float(data.get("resultMultiplier", data.get("result", _nested_result(data))))
    return {
        "amount": float(data.get("amount", 0)),
        "payout": float(data.get("payout", 0)),
        "payout_mult": payout_mult,
        "result_display": f"{result:.4f}x",
        "result_value": result,
        "is_win": payout_mult > 0,
        "raw_state": data,
    }

_register_game("limbo", "Limbo", "/limbo/bet", "limboBet", _limbo_payload, _limbo_parse)


# -- DICE --
def _dice_payload(st):
    return {
        "target": st.dice_target,
        "condition": st.dice_condition,
        "identifier": _gen_identifier(),
        "amount": round(st.current_bet, 8) if st.current_bet >= MIN_BET else st.current_bet,
        "currency": st.currency,
    }

def _dice_parse(data):
    payout_mult = float(data.get("payoutMultiplier", 0))
    result = float(data.get("resultTarget", data.get("result", _nested_result(data))))
    return {
        "amount": float(data.get("amount", 0)),
        "payout": float(data.get("payout", 0)),
        "payout_mult": payout_mult,
        "result_display": f"{result:.2f}",
        "result_value": result,
        "is_win": payout_mult > 0,
        "raw_state": data,
    }

_register_game("dice", "Dice", "/dice/roll", "diceRoll", _dice_payload, _dice_parse)


# ===========================================================
#  DATABASE SETUP
# ===========================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    TEXT,
            ended_at      TEXT,
            currency      TEXT,
            game          TEXT,
            strategy      TEXT,
            base_bet      REAL,
            multiplier    REAL,
            total_bets    INTEGER DEFAULT 0,
            wins          INTEGER DEFAULT 0,
            losses        INTEGER DEFAULT 0,
            profit        REAL    DEFAULT 0,
            wagered       REAL    DEFAULT 0,
            start_balance REAL,
            end_balance   REAL,
            max_win_streak  INTEGER DEFAULT 0,
            max_loss_streak INTEGER DEFAULT 0,
            highest_balance REAL DEFAULT 0,
            lowest_balance  REAL DEFAULT 0,
            highest_win     REAL DEFAULT 0,
            biggest_loss    REAL DEFAULT 0,
            bets_per_minute REAL DEFAULT 0,
            bets_per_second REAL DEFAULT 0,
            peak_bps        REAL DEFAULT 0,
            low_bps         REAL DEFAULT 0,
            peak_bpm        REAL DEFAULT 0,
            low_bpm         REAL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER,
            timestamp    TEXT,
            game         TEXT,
            amount       REAL,
            multiplier_target REAL,
            result_value REAL,
            result_display TEXT,
            state        TEXT,
            profit       REAL,
            balance_after REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    # Migrate: add game and result_display columns if missing
    for col, defn in [
        ("game", "TEXT DEFAULT 'limbo'"),
        ("result_display", "TEXT DEFAULT ''"),
    ]:
        try:
            c.execute(f"ALTER TABLE bets ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass
    ensure_sessions_lane(conn)  # v1.11.0 -- sessions.lane (WolfBet v2.35.0)
    conn.commit()
    conn.close()

# ===========================================================
#  CONFIG PERSISTENCE
# ===========================================================
_CONFIG_KEYS = [
    "access_token", "lockdown_token", "cookie", "currency", "game",
    "multiplier_target", "dice_target", "dice_condition",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "profit_increment", "profit_threshold", "profit_increment_scope", "proxy",
    # v1.10.0 -- WolfBet v2.16.0 / v2.28.2 / v2.32.0 session behaviour
    "recurring", "recurring_delay", "streak_reset_losses", "streak_reset_delay",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "basebet_pct", "milestone_bets", "milestone_wins", "milestone_losses",
    "milestone_profit",
    # v1.12.0 -- WolfBet v2.26.0 retention / v2.27.0 async bet writer
    "purge_days", "db_write_mode",
    "key_ref", "proxy_ref",
]

# Stored as "N:X" strings (None when off), held as (N, X) tuples in state.
_STREAK_TUPLE_KEYS = ["streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss"]
# Delays clamped to 0..RECURRING_DELAY_MAX on load (WolfBet v2.30.6).
_DELAY_KEYS = ["recurring_delay", "streak_reset_delay"]

def _config_value(k: str):
    """State value as written to the config / preset file."""
    v = getattr(state, k)
    if k in _STREAK_TUPLE_KEYS:
        n, x = v
        return f"{n}:{x}" if n > 0 else None
    return v

def _set_config_value(k: str, v):
    """Apply one value read from the config / preset file to state."""
    if k in _STREAK_TUPLE_KEYS:
        v = parse_streak_delay(v)
        if k == "streakbet_loss" and v[0] == 0:
            v = (0, 1.0)                 # off: neutral factor, same as the default
    elif k in _DELAY_KEYS:
        v, msg = clamp(k, float(v or 0), 0.0, RECURRING_DELAY_MAX)
        if msg:
            logger.warning(msg)
    elif k == "proxy":
        try:
            v = validate_proxy(v) or ""
        except ValueError as e:
            logger.warning("saved proxy ignored: %s", e)
            v = ""
    elif k == "purge_days":
        try:
            v = int(max(1, min(30, float(v))))
        except (TypeError, ValueError):
            v = 3
    elif k == "db_write_mode":
        v = v if v in ("async", "sync") else "async"
    elif k == "profit_increment_scope":
        try:
            v = parse_scope(v)
        except ValueError:
            logger.warning("saved profit_increment_scope ignored: %r", v)
            v = "session"
    elif k in ("profit_increment", "profit_threshold"):
        # v1.15.0 -- 0 / negative in the config file means off, as `none` does.
        try:
            v = float(v) if v is not None and float(v) > 0 else None
        except (TypeError, ValueError):
            v = None
    setattr(state, k, v)

def _apply_loaded(data: dict, keys: list):
    """Set every present key, then re-arm what depends on them."""
    for k in keys:
        if k in data:
            _set_config_value(k, data[k])
    state.current_bet = state.base_bet
    if state.profit_threshold:
        state.next_profit_milestone = state.profit_threshold
    if state.custom_rules_text:
        state.custom_rules = _load_rules_from_text(state.custom_rules_text)

def _mask_key(key: str) -> str:
    """Mask token/key for safe logging: show first 8 + last 4 chars."""
    if not key or len(key) < 16:
        return "***"
    return f"{key[:8]}...{key[-4:]}"

def save_config():
    if state.lane > 1:
        # v1.13.0 (wolfbet v2.35.0) -- a lane's state holds base + its overrides;
        # writing it back would leak them into the shared base config.
        logger.info("save_config skipped on lane %d (base config is lane 1's)", state.lane)
        return
    old = _read_config_raw() or {}
    data = {k: _config_value(k) for k in _CONFIG_KEYS}
    # v1.13.0 (wolfbet v2.36.0) -- an untouched split base bet is saved as the full one.
    if state.base_bet_split is not None and state.base_bet == state.base_bet_split:
        data["base_bet"] = state.base_bet_disk
    # v1.13.0 (wolfbet v2.35.0) -- keep the lanes block (count, split, overrides).
    if "lanes" in old:
        data["lanes"] = old["lanes"]
    # A vault proxy carries a password: the file keeps its own proxy value, never the vault's.
    if state.proxy_ref:
        data["proxy"] = old.get("proxy")
    # The login lives in the vault once it reads back equal; the file keeps a copy only while
    # the vault can't take it, and never another account's secrets.
    if _store_account_in_vault():
        if any(f in old for f in ACCOUNT_FIELDS):
            _backup_config_once(".pre-vault", "moving the login to the vault")
        for f in ACCOUNT_FIELDS:
            data.pop(f, None)
    elif state.key_ref != VAULT_KEY_ID:
        for f in ACCOUNT_FIELDS:
            if f in old:
                data[f] = old[f]
            else:
                data.pop(f, None)
    _write_config_raw(data)
    logger.info("Config saved (token=%s)", _mask_key(state.access_token))


def _account_fields() -> dict:
    return {f: getattr(state, f) or "" for f in ACCOUNT_FIELDS}


def _active_account(config: dict, key_ref: str = VAULT_KEY_ID) -> Optional[dict]:
    """The login this lane bets with. For `default`: the config's copy (only there while the
    vault can't take it), else the vault. Any other account comes from the vault only."""
    if key_ref == VAULT_KEY_ID and config.get("access_token"):
        return {f: config.get(f) or "" for f in ACCOUNT_FIELDS}
    return vault_account(VAULT_SERVICE, key_ref, log=logger)


def _vault_proxy(merged: dict):
    """(proxy URL, problem): the vault proxy a lane names with proxy_ref, else its own proxy."""
    ref = merged.get("proxy_ref")
    if not ref:
        return merged.get("proxy"), None
    url = vault_secret(VAULT_SERVICE, ref, "proxy", log=logger)
    if url is None:
        return None, f"Lane {state.lane}: proxy {ref} is not in the vault"
    return url, None


def _store_account_in_vault() -> bool:
    """Make the vault's default equal the login in use (lane 1 only). True once it reads back equal."""
    if state.lane > 1 or not state.access_token or state.key_ref != VAULT_KEY_ID:
        return False
    fields = _account_fields()
    try:
        vault = Vault(VAULT_SERVICE)
        if vault.account(VAULT_KEY_ID) != fields:
            vault.put_account(VAULT_KEY_ID, fields, "Stake main login", primary="access_token")
            logger.info("Stake login stored in the vault as %s", VAULT_KEY_ID)
        return vault.account(VAULT_KEY_ID) == fields
    except (VaultError, OSError) as e:
        logger.warning("Key vault unavailable: %s", e)
        return False


def _sync_vault_account() -> None:
    """Move the login into the vault, then drop the config file's plaintext copy (one backup kept)."""
    if not _store_account_in_vault():
        return
    raw = _read_config_raw() or {}
    if not any(f in raw for f in ACCOUNT_FIELDS):
        return
    _backup_config_once(".pre-vault", "moving the login to the vault")
    for f in ACCOUNT_FIELDS:
        raw.pop(f, None)
    _write_config_raw(raw)
    logger.info("Stake login removed from %s; the vault keeps it", CONFIG_PATH)


def _apply_lane_account(data: dict, merged: dict) -> None:
    """This lane's login, vault proxy and same-account split, read at start: a lane whose key
    or proxy is missing gets an `account_problem` instead of another account's login."""
    state.split_lanes = account_lanes(data, state.lane, _LANE_FORBIDDEN_KEYS, VAULT_KEY_ID)
    state.key_ref = merged.get("key_ref") or VAULT_KEY_ID
    state.proxy_ref = merged.get("proxy_ref") or None
    problem = None
    if state.proxy_ref:                   # a plain proxy was already checked on load
        state.proxy, problem = _vault_proxy(merged)
        state.proxy = state.proxy or ""
    fields = _active_account(data, state.key_ref)
    if fields is None and state.key_ref != VAULT_KEY_ID:
        fields = {f: "" for f in ACCOUNT_FIELDS}
        problem = problem or f"Lane {state.lane}: key {state.key_ref} is not in the vault"
    for f, v in (fields or {}).items():
        if f in ACCOUNT_FIELDS:
            setattr(state, f, v)
    state.account_problem = problem

def _apply_lane_config(data: dict) -> dict:
    """v1.13.0 (wolfbet v2.35.0 / v2.36.0) -- read the lane count and split mode
    and return the config this lane runs: base + its overrides + bet split."""
    state.lanes_count = lanes_count(data)
    state.lane_split = lane_split(data)
    # v1.14.0 (wolfbet v2.36.0) -- a split bet below MIN_BET stays full.
    merged, warning = lane_config(data, state.lane, _LANE_FORBIDDEN_KEYS, MIN_BET)
    if warning:
        logger.warning(warning)
    _remember_config_base(data, merged)
    return merged


def _remember_config_base(data: dict, merged: dict) -> None:
    """Keep the file's base and this lane's split base apart from the live, bumped one."""
    state.base_bet_disk = data.get("base_bet")
    state.base_bet_split = (merged.get("base_bet")
                            if merged.get("base_bet") != data.get("base_bet") else None)


def _configured_base():
    return state.base_bet_split if state.base_bet_split is not None else state.base_bet_disk

def load_config(rebuild_http: bool = True) -> bool:
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        merged = _apply_lane_config(data)
        _apply_loaded(merged, _CONFIG_KEYS)
        _apply_lane_account(data, merged)
    except Exception:
        return False
    # v1.12.0 fix: the saved proxy only reached the HTTP client by accident
    # (a CF profile rotation). Rebuild it here so --resume / --daemon /
    # --preset and every command that loads the config use the proxy.
    if rebuild_http:
        _recreate_http()
    return True

# Keys SIGHUP may change on a running bot (WolfBet v2.28.3). Everything else
# (credentials, currency, game, multiplier, strategy) needs a restart.
def _reload_live_config() -> dict:
    """Re-read CONFIG_PATH and apply only live-safe keys. Returns {key: (old, new)}."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    # v1.13.0 (wolfbet v2.35.0 / v2.36.0) -- this lane's overrides + bet split.
    raw, data = data, lane_config(data, state.lane, _LANE_FORBIDDEN_KEYS, MIN_BET)[0]
    data["proxy"] = _vault_proxy(data)[0] or ""
    state.lanes_count = lanes_count(raw)
    state.split_lanes = account_lanes(raw, state.lane, _LANE_FORBIDDEN_KEYS, VAULT_KEY_ID)
    _set_lane_log_prefix(state.lane, lane_name(raw, state.lane))
    changed = {}
    with state.lock:
        for k in _LIVE_RELOAD_KEYS:
            if k not in data or k == "base_bet":
                continue
            old = getattr(state, k)
            _set_config_value(k, data[k])
            new = getattr(state, k)
            if new != old:
                changed[k] = (old, new)
            if (k == "profit_threshold" and k in changed and new
                    and state.profit_increment_scope != "run"):
                state.next_profit_milestone = new
        # The live base carries bumps, so a change is judged against the configured base.
        new_base, old_base = data.get("base_bet"), _configured_base()
        if old_base is None:
            old_base = state.base_bet
        if new_base is not None and abs(float(new_base) - float(old_base)) > 1e-12:
            _remember_config_base(raw, data)
            changed["base_bet"] = (state.base_bet, new_base)
            _rebase_run_for_base_change(new_base)
        # v1.15.0 -- in run scope an increment change ends (off) or starts a run.
        if state.profit_increment_scope == "run" and (
                "profit_increment" in changed or "profit_threshold" in changed):
            if not _increment_on():
                _end_run("increment off")
            elif state.run_first_session is None and state.session_id:
                _attach_run(state.session_id)
                state.next_profit_milestone = state.profit_threshold
    skipped = [k for k in _CONFIG_KEYS
               if k not in _LIVE_RELOAD_KEYS and k in data and data[k] != _config_value(k)]
    state.pending_restart = {k: [_config_value(k), data[k]] for k in sorted(skipped)
                             if k in _SETTINGS and k not in _PENDING_HIDDEN}
    if skipped:
        logger.warning("live reload: skipped %d unsafe field(s) -- restart daemon to apply: %s",
                       len(skipped), ", ".join(skipped))
    if "custom_rules_text" in changed:
        state.custom_rules = _load_rules_from_text(state.custom_rules_text)
    if "proxy" in changed:
        _recreate_http()
        _cf_switch_ip()
    return changed

def _sig_reload(sig, frame):
    try:
        changed = _reload_live_config()
        if changed:
            logger.warning("SIGHUP: reloaded %d live field(s): %s",
                           len(changed), ", ".join(changed))
        else:
            logger.warning("SIGHUP: no live-safe changes to apply")
    except Exception as e:
        logger.error("SIGHUP reload failed: %s", e)

# ===========================================================
#  PRESETS
# ===========================================================
_PRESET_KEYS = [
    "currency", "game", "multiplier_target", "dice_target", "dice_condition",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "profit_increment", "profit_threshold", "profit_increment_scope",
    "recurring", "recurring_delay", "streak_reset_losses", "streak_reset_delay",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "basebet_pct", "milestone_bets", "milestone_wins", "milestone_losses",
    "milestone_profit",
    # v1.12.0 -- WolfBet v2.26.0 / v2.27.0 (proxy stays out: a preset must
    # not swap the network path the CF cookies were solved on)
    "purge_days", "db_write_mode",
]

def _load_all_presets() -> dict:
    if not os.path.exists(PRESET_PATH):
        return {}
    try:
        with open(PRESET_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_all_presets(presets: dict):
    fd = os.open(PRESET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(presets, f, indent=2)

def save_preset(name: str):
    presets = _load_all_presets()
    presets[name] = {k: _config_value(k) for k in _PRESET_KEYS}
    _save_all_presets(presets)

def load_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    _apply_loaded(presets[name], _PRESET_KEYS)
    return True

def delete_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    del presets[name]
    _save_all_presets(presets)
    return True

def list_presets() -> dict:
    return _load_all_presets()

# ===========================================================
#  RULE-BASED STRATEGY ENGINE
# ===========================================================

COND_TYPES = {
    "1": "sequence",
    "2": "profit",
    "3": "bet",
}

SEQ_MODES = {
    "1": ("every",         "Every N (total count)"),
    "2": ("every_streak",  "Every N in streak (N, 2N, 3N...)"),
    "3": ("first_streak",  "First streak of N (once, resets on flip)"),
    "4": ("streak_above",  "Streak above N (every bet past N)"),
    "5": ("streak_below",  "Streak below N"),
}
SEQ_TRIGGERS = {"1": "win", "2": "loss", "3": "bet"}

PROFIT_FIELDS = {"1": "profit", "2": "loss", "3": "balance"}

BET_FIELDS = {"1": "amount", "2": "number", "3": "winchance", "4": "payout"}

CMP_OPS = {
    "1": ("gte", ">="),
    "2": ("gt",  ">"),
    "3": ("lte", "<="),
    "4": ("lt",  "<"),
}

RULE_ACTIONS = {
    "1":  ("reset_amount",    "Reset bet amount"),
    "2":  ("increase_amount", "Increase amount by %"),
    "3":  ("decrease_amount", "Decrease amount by %"),
    "4":  ("add_amount",      "Add to amount"),
    "5":  ("deduct_amount",   "Deduct from amount"),
    "6":  ("set_amount",      "Set amount"),
    "7":  ("switch",          "Switch above/below (dice only)"),
    "8":  ("stop",            "Stop betting"),
    "9":  ("set_winchance",   "Set win chance (changes multiplier)"),
    "10": ("increase_wc",     "Increase win chance by %"),
    "11": ("decrease_wc",     "Decrease win chance by %"),
    "12": ("reset_game",      "Reset game (full reset)"),
}


class StrategyRule:
    __slots__ = ("cond_type", "cond_field", "cond_mode", "cond_value",
                 "cond_trigger", "action", "action_value", "description")

    def __init__(self, cond_type="", cond_field="", cond_mode="", cond_value=0.0,
                 cond_trigger="", action="", action_value=0.0, description=""):
        self.cond_type    = cond_type
        self.cond_field   = cond_field
        self.cond_mode    = cond_mode
        self.cond_value   = cond_value
        self.cond_trigger = cond_trigger
        self.action       = action
        self.action_value = action_value
        self.description  = description

    def __repr__(self):
        return f"Rule({self.description})"

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyRule":
        return cls(**{k: d.get(k, "") for k in cls.__slots__})


def _describe_rule(r: StrategyRule) -> str:
    if r.cond_type == "sequence":
        mode_label = dict(every="Every", every_streak="Every streak of",
                          first_streak="First streak of",
                          streak_above="Streak above", streak_below="Streak below").get(r.cond_mode, r.cond_mode)
        cond = f"{mode_label} {int(r.cond_value)} {r.cond_trigger}(s)"
    elif r.cond_type == "profit":
        op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
        cond = f"On {r.cond_field} {op_sym} {r.cond_value}"
    elif r.cond_type == "bet":
        op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
        cond = f"On bet {r.cond_field} {op_sym} {r.cond_value}"
    else:
        cond = "?"

    act_labels = {k: v[1] for k, v in RULE_ACTIONS.items()}
    act_name = act_labels.get(
        next((k for k, v in RULE_ACTIONS.items() if v[0] == r.action), ""), r.action
    )
    if r.action_value and "%" in act_name:
        act = f"{act_name.replace(' %', '')} {r.action_value}%"
    elif r.action_value:
        act = f"{act_name} {r.action_value}"
    else:
        act = act_name

    return f"{cond} -> {act}"


def _cmp(actual: float, mode: str, threshold: float) -> bool:
    if mode == "gte": return actual >= threshold
    if mode == "gt":  return actual > threshold
    if mode == "lte": return actual <= threshold
    if mode == "lt":  return actual < threshold
    return False


def _get_win_chance() -> float:
    """Calculate current win chance based on game type."""
    if state.game == "dice":
        if state.dice_condition == "above":
            return 100.0 - state.dice_target
        else:
            return state.dice_target
    else:  # limbo
        return 99.0 / state.multiplier_target if state.multiplier_target > 0 else 0


def _apply_action(rule: StrategyRule):
    a = rule.action
    v = rule.action_value

    if a == "reset_amount":
        state.current_bet = state.base_bet
    elif a == "increase_amount":
        state.current_bet *= (1 + v / 100)
    elif a == "decrease_amount":
        state.current_bet *= (1 - v / 100)
        state.current_bet = max(state.current_bet, state.base_bet)
    elif a == "add_amount":
        state.current_bet += v
    elif a == "deduct_amount":
        state.current_bet = max(state.base_bet, state.current_bet - v)
    elif a == "set_amount":
        state.current_bet = max(state.base_bet, v)
    elif a == "switch":
        # Switch above/below for dice
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_condition = "below"
                state.dice_target = round(99.0 / state.multiplier_target, 2)
            else:
                state.dice_condition = "above"
                state.dice_target = round(100.0 - 99.0 / state.multiplier_target, 2)
            logger.debug("RULE SWITCH -> %s  target=%s", state.dice_condition, state.dice_target)
    elif a == "stop":
        state.running = False
        state.status = f"Rule stop: {rule.description}"
        logger.warning("RULE STOP triggered: %s", rule.description)
    elif a == "set_winchance":
        wc = max(0.01, min(98.99, v))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
        logger.debug("RULE SET WC -> %.2f%%  mult=%.4f", wc, state.multiplier_target)
    elif a == "increase_wc":
        wc = _get_win_chance()
        wc = min(98.99, wc * (1 + v / 100))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "decrease_wc":
        wc = _get_win_chance()
        wc = max(0.01, wc * (1 - v / 100))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "reset_winchance":
        state.multiplier_target = state.initial_multiplier
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "add_wc":
        wc = _get_win_chance()
        wc = max(0.01, min(98.99, wc + v))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "deduct_wc":
        wc = _get_win_chance()
        wc = max(0.01, min(98.99, wc - v))
        state.multiplier_target = round(99.0 / wc, 4)
        if state.game == "dice":
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "reset_payout":
        state.multiplier_target = state.initial_multiplier
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "set_payout":
        state.multiplier_target = max(1.01, v)
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "increase_payout":
        state.multiplier_target = state.multiplier_target * (1 + v / 100)
        if state.game == "dice":
            wc = max(0.01, 99.0 / state.multiplier_target)
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "decrease_payout":
        state.multiplier_target = max(1.01, state.multiplier_target * (1 - v / 100))
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "add_payout":
        state.multiplier_target = state.multiplier_target + v
        if state.game == "dice":
            wc = max(0.01, 99.0 / state.multiplier_target)
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "deduct_payout":
        state.multiplier_target = max(1.01, state.multiplier_target - v)
        if state.game == "dice":
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
    elif a == "reset_game":
        state.current_bet = state.base_bet
        state.current_streak = 0
        logger.info("RULE RESET GAME triggered: %s", rule.description)


def apply_rules(bet_state: str):
    for rule in state.custom_rules:
        triggered = False

        if rule.cond_type == "sequence":
            streak = state.current_streak
            trig = rule.cond_trigger
            n = rule.cond_value

            if rule.cond_mode == "every":
                if trig == "win" and bet_state == "win":
                    triggered = (state.wins > 0 and state.wins % int(n) == 0)
                elif trig == "loss" and bet_state == "loss":
                    triggered = (state.losses > 0 and state.losses % int(n) == 0)
                elif trig == "bet":
                    triggered = (state.total_bets > 0 and state.total_bets % int(n) == 0)
            elif rule.cond_mode == "every_streak":
                # Fires at every Nth bet in the streak (N, 2N, 3N...)
                if trig == "win" and bet_state == "win":
                    triggered = (streak > 0 and streak % int(n) == 0)
                elif trig == "loss" and bet_state == "loss":
                    triggered = (abs(streak) > 0 and abs(streak) % int(n) == 0)
            elif rule.cond_mode == "first_streak":
                # Fires ONCE when streak first hits N, silent until streak resets
                if trig == "win" and bet_state == "win":
                    triggered = (streak == int(n))
                elif trig == "loss" and bet_state == "loss":
                    triggered = (abs(streak) == int(n))
            elif rule.cond_mode == "streak_above":
                if trig == "win" and bet_state == "win":
                    triggered = (streak > int(n))
                elif trig == "loss" and bet_state == "loss":
                    triggered = (abs(streak) > int(n))
            elif rule.cond_mode == "streak_below":
                if trig == "win" and bet_state == "win":
                    triggered = (streak < int(n))
                elif trig == "loss" and bet_state == "loss":
                    triggered = (abs(streak) < int(n))

        elif rule.cond_type == "profit":
            field = rule.cond_field
            if field == "profit" and state.profit > 0:
                triggered = _cmp(state.profit, rule.cond_mode, rule.cond_value)
            elif field == "loss" and state.profit < 0:
                triggered = _cmp(abs(state.profit), rule.cond_mode, rule.cond_value)
            elif field == "balance":
                triggered = _cmp(state.current_balance, rule.cond_mode, rule.cond_value)

        elif rule.cond_type == "bet":
            field = rule.cond_field
            if field == "amount":
                triggered = _cmp(state.current_bet, rule.cond_mode, rule.cond_value)
            elif field == "number":
                triggered = _cmp(state.total_bets, rule.cond_mode, rule.cond_value)
            elif field == "winchance":
                wc = _get_win_chance()
                triggered = _cmp(wc, rule.cond_mode, rule.cond_value)
            elif field == "payout":
                triggered = _cmp(state.multiplier_target, rule.cond_mode, rule.cond_value)

        if triggered:
            logger.debug("RULE FIRED: %s", rule.description)
            _apply_action(rule)


def _load_rules_from_text(text: str) -> List[StrategyRule]:
    text = text.strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            data = json.loads(text)
            rules = []
            for d in data:
                r = StrategyRule.from_dict(d)
                if not r.description:
                    r.description = _describe_rule(r)
                rules.append(r)
            return rules
        except (json.JSONDecodeError, TypeError):
            pass

    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(
            r'onEvery(\d+)(win|lose|bet)(Reset|Stop|Increase|Switch)\s*(.*)',
            line, re.IGNORECASE
        )
        if not m:
            logger.warning("Could not parse legacy rule: %s", line)
            continue
        n       = int(m.group(1))
        trigger = m.group(2).lower()
        action  = m.group(3).lower()
        params  = m.group(4).strip()
        value   = 0.0

        if action == "increase":
            pct = re.search(r'(\d+(?:\.\d+)?)%', params)
            if pct:
                value = float(pct.group(1))

        action_map = {"reset": "reset_amount", "stop": "stop",
                      "increase": "increase_amount", "switch": "switch"}
        r = StrategyRule(
            cond_type="sequence", cond_mode="every", cond_value=float(n),
            cond_trigger=trigger if trigger != "lose" else "loss",
            action=action_map.get(action, action), action_value=value,
        )
        r.description = _describe_rule(r)
        rules.append(r)

    return rules


# ===========================================================
#  BOT STATE
# ===========================================================
class BotState:
    def __init__(self):
        self.lock = threading.Lock()

        # -- runtime flags --
        self.running  = False
        self.paused   = False
        self.stopping = False

        # -- config --
        self.access_token     = ""
        self.lockdown_token   = ""
        self.cookie           = ""     # full cookie string from browser
        self.proxy            = ""     # optional proxy: http://user:pass@host:port or socks5://...
        self.currency         = "usdt"
        self.game             = "limbo"       # "limbo" or "dice"
        self.multiplier_target = 2.0          # shared: target payout multiplier
        self.initial_multiplier = 2.0         # saved at session start for reset actions
        # dice-specific
        self.dice_target      = 50.5          # roll target (0-100)
        self.dice_condition   = "above"       # "above" or "below"

        self.base_bet         = 0.0
        self.current_bet      = 0.0
        self.strategy         = "Martingale"
        self.strategy_key     = "2"
        self.win_mult         = 1.0
        self.loss_mult        = 2.0

        # -- rule-based strategy --
        self.custom_rules: List[StrategyRule] = []
        self.custom_rules_text: str = ""

        # -- stop conditions --
        self.max_profit      : Optional[float] = None
        self.max_loss        : Optional[float] = None
        self.max_bets        : Optional[int]   = None
        self.max_wins        : Optional[int]   = None
        self.stop_on_balance : Optional[float] = None

        # -- session info --
        self.session_id     = None
        self.session_start  = time.time()
        self.total_bets     = 0
        self.wins           = 0
        self.losses         = 0
        self.profit         = 0.0
        self.wagered        = 0.0
        self.start_balance  = 0.0
        self.current_balance= 0.0

        # -- streaks --
        self.current_streak  = 0
        self.max_win_streak  = 0
        self.max_loss_streak = 0

        # -- extremes --
        self.highest_bet     = 0.0
        self.highest_win     = 0.0
        self.biggest_loss    = 0.0
        self.highest_balance = 0.0
        self.lowest_balance  = float("inf")

        # -- bet pacing --
        self.bet_delay       = 0
        self.backoff_delay   = 1.0
        self.max_backoff     = 30.0
        self.consecutive_errors = 0
        self._insufficient_balance = False

        # -- performance --
        self.bets_per_minute = 0.0
        self.bets_per_second = 0.0
        self.peak_bps        = 0.0
        self.low_bps         = float("inf")
        self.peak_bpm        = 0.0
        self.low_bpm         = float("inf")
        self._bets_this_sec  = 0
        self._bets_this_min  = 0
        self._current_sec    = 0
        self._current_min    = 0

        # -- v1.12.0: API / cycle observability (WolfBet v2.17.0, v2.30.3-5) --
        self._last_api_ms      = 0.0
        self._api_ms_total     = 0.0
        self._api_ms_count     = 0
        self.peak_api_ms       = 0.0
        self._api_samples      = deque(maxlen=500)
        self._on_target_count  = 0
        self._cycle_ms         = 0.0     # alias of cycle p50
        self._cycle_overhead_ms = 0.0
        self._cycle_samples    = deque(maxlen=500)

        # -- v1.12.0: retention + DB writer (WolfBet v2.26.0 / v2.27.0) --
        self.purge_days    : int = 3
        self.db_write_mode : str = "async"

        # -- profit history for sparkline --
        self.profit_history  = deque(maxlen=80)
        self.profit_history.append(0.0)

        # -- recent bets --
        self.recent_bets = deque(maxlen=10)

        # -- status messages --
        self.status     = "Initializing..."
        self.last_error = ""

        # -- profit-based base bet increment --
        self.profit_increment    : Optional[float] = None
        self.profit_threshold    : Optional[float] = None
        self.next_profit_milestone: float = 0.0
        # v1.15.0 -- run-wide increment: profit summed across sessions since
        # the run started (kept in RUN_PATH until `--run-reset`).
        self.profit_increment_scope = "session"
        self.run_first_session: Optional[int] = None
        self.run_since        : Optional[str] = None
        self.run_bumps        = 0
        self.run_profit_done  = 0.0
        self.run_base_origin: Optional[float] = None  # base the bumps were earned on

        # -- v1.10.0: WolfBet session behaviour (v2.16.0 .. v2.32.0) --
        self.streak_delay_loss: tuple = (0, 0.0)   # (N, sec): sleep after every N-loss streak
        self.streak_delay_win : tuple = (0, 0.0)
        self.streak_delay_bets: tuple = (0, 0.0)   # every N bets
        self.streakbet_loss   : tuple = (0, 1.0)   # (N, mult): next bet x mult every N losses
        self.basebet_pct      : float = 0.0        # base bet = balance x pct, re-checked every 10 bets
        self.milestone_bets   : int   = 100
        self.milestone_wins   : int   = 0
        self.milestone_losses : int   = 0
        self.milestone_profit : float = 0.0
        self._last_profit_milestone: float = 0.0
        self.recurring        : bool  = False      # restart a new session after a safe stop
        self.recurring_delay  : float = 60.0
        self.streak_reset_losses: Optional[int] = None  # stop+restart after N losses then a win
        self.streak_reset_delay : float = 600.0
        self.last_broken_loss_streak: int = 0
        # pause between cycles: uptime freezes at the bet end
        self.session_bet_ended_at    : Optional[float] = None
        self.session_pause_started_at: Optional[float] = None
        self.session_pause_total     : float = 0.0
        self.run_started_at          : float = time.time()   # never reset across cycles

        # -- v1.13.0 parallel lanes (wolfbet v2.35.0 / v2.36.0) --
        # lane_share None = no split budget. lane_run_profit carries the
        # lane's result across its sessions (recurring / streak-reset cycles).
        self.lane            = 1
        self.lanes_count     = 1
        self.split_lanes     = 1          # enabled lanes on this lane's account (key + currency)
        self.key_ref         = "default"
        self.proxy_ref       : Optional[str] = None
        self.account_problem : Optional[str] = None
        self.pending_restart : dict = {}   # saved restart-only changes: {key: [running, saved]}
        self.lane_split      = "none"     # see LANE_SPLIT_MODES
        self.lane_share      : Optional[float] = None
        self.lane_run_profit = 0.0
        self.base_bet_disk   = None       # base_bet as saved in the config file
        self.base_bet_split  = None       # this lane's split base bet (None = no split)

        # -- strategy internal state --
        self.dalembert_unit = 0
        self.paroli_count   = 0
        self.delay_martin_threshold = 3


state   = BotState()
console = Console()


# ===========================================================
#  OBSERVABILITY METRICS  (WolfBet v2.17.0, v2.30.3-2.30.5)
# ===========================================================
# API time = one bet HTTP round trip. Cycle time = one betting_loop
# iteration (delays + bet + bookkeeping). Both keep a 500-sample window;
# p50/p99/mean and efficiency come from the same window. Stake sends no
# rate-limit headers, so there is no rate-limit row (N/A).
def _compute_api_avg() -> float:
    """All-time mean API time in ms (0 when no samples yet)."""
    if state._api_ms_count == 0:
        return 0.0
    return state._api_ms_total / state._api_ms_count


def _compute_api_p50() -> float:
    """Median API time in ms from the last-500-sample window (0 when empty)."""
    if not state._api_samples:
        return 0.0
    ordered = sorted(state._api_samples)
    return ordered[len(ordered) // 2]


def _compute_api_p99() -> float:
    """99th-percentile API time in ms from the window (0 when empty)."""
    return percentile(sorted(state._api_samples), 0.99)


def _compute_on_target_pct() -> Optional[float]:
    """% of bets whose API time <= bet_delay; None when no delay / no samples."""
    if state.bet_delay <= 0 or state._api_ms_count == 0:
        return None
    return (state._on_target_count / state._api_ms_count) * 100


def _compute_api_rolling_avg() -> float:
    """Mean API ms over the same window as p50/p99 (0 when empty)."""
    if not state._api_samples:
        return 0.0
    return sum(state._api_samples) / len(state._api_samples)


def _compute_cycle_p50() -> float:
    """Median per-iteration cycle time in ms (0 when empty)."""
    if not state._cycle_samples:
        return 0.0
    ordered = sorted(state._cycle_samples)
    return ordered[len(ordered) // 2]


def _compute_cycle_p99() -> float:
    """99th-percentile per-iteration cycle time in ms (0 when empty)."""
    return percentile(sorted(state._cycle_samples), 0.99)


def _compute_cycle_mean() -> float:
    """Mean per-iteration cycle time in ms (0 when empty). Drives Recent BPS."""
    if not state._cycle_samples:
        return 0.0
    return sum(state._cycle_samples) / len(state._cycle_samples)


def _compute_recent_bps() -> float:
    """Windowed BPS = 1000 / cycle mean (same statistic as the ceiling)."""
    cycle_mean = _compute_cycle_mean()
    return 1000.0 / cycle_mean if cycle_mean > 0 else 0.0


def _compute_theoretical_max_bps() -> float:
    """Ceiling BPS = 1 / max(bet_delay, rolling api avg). 0 when both are 0."""
    api_avg_s = _compute_api_rolling_avg() / 1000.0
    floor = max(state.bet_delay, api_avg_s)
    return (1.0 / floor) if floor > 0 else 0.0


def _compute_efficiency_pct() -> float:
    """Recent BPS as % of the ceiling, capped at 100. Session BPS until the window fills."""
    tmax = _compute_theoretical_max_bps()
    if tmax <= 0:
        return 0.0
    recent = _compute_recent_bps()
    if recent <= 0:
        recent = state.bets_per_second
    return min(100.0, recent / tmax * 100)


def _record_api_ms(dt_ms: float) -> None:
    """Update every API-timing counter from one request."""
    with state.lock:
        state._last_api_ms   = dt_ms
        state._api_ms_total += dt_ms
        state._api_ms_count += 1
        state._api_samples.append(dt_ms)
        if dt_ms > state.peak_api_ms:
            state.peak_api_ms = dt_ms
        if state.bet_delay > 0 and dt_ms <= state.bet_delay * 1000:
            state._on_target_count += 1


def _record_cycle_ms(cycle_observed_ms: float) -> None:
    """Push one cycle observation; overhead = cycle p50 - api p50 - delay."""
    with state.lock:
        state._cycle_samples.append(cycle_observed_ms)
        state._cycle_ms = _compute_cycle_p50()
        api_ref = _compute_api_p50() if state._api_samples else state._last_api_ms
        state._cycle_overhead_ms = max(0.0, state._cycle_ms - api_ref - state.bet_delay * 1000)


# ===========================================================
#  RISKY CONFIG HINTS  (WolfBet v2.22.1 / v2.32.1)
# ===========================================================
# Stake keys: 2 = Martingale, 6 = Delay Martingale (WolfBet "7"),
# 7 = Rule-Based (WolfBet "8"). Hints only, never blocking.
_DOUBLING_KEYS = ("2", "6")
_RULES_KEY = "7"


def martingale_chain_depth(balance: float, base: float, mult: float) -> int:
    """Longest loss run a Martingale chain can pay for: max k with
    base*(mult^k - 1)/(mult - 1) <= balance. 0 when inputs can't form a chain."""
    if balance <= 0 or base <= 0 or mult <= 1:
        return 0
    k = int(math.log(1 + balance * (mult - 1) / base) / math.log(mult))
    while k > 0 and base * (mult ** k - 1) / (mult - 1) > balance:
        k -= 1
    return k


def _check_risky_config() -> list:
    """Return human-readable heads-up strings for combinations that drain balance fast."""
    warnings = []
    sk = state.strategy_key
    mult = state.multiplier_target
    loss_mult = state.loss_mult

    if sk == "2" and loss_mult >= 2.5:
        warnings.append(
            f"Aggressive Martingale (loss_mult={loss_mult}) -- a 5-loss streak needs "
            f"~{loss_mult**5:.0f}x base_bet in reserve."
        )
    if sk in _DOUBLING_KEYS and state.max_loss is None:
        warnings.append(
            "No `max_loss` set -- Martingale-style strategies can drain balance "
            "on a bad streak. Consider `maxloss <amount>` (T hotkey or --config-set)."
        )
    # v1.13.0 (wolfbet v2.35.0 / v2.36.0) -- with a split budget a lane may
    # only risk its share of the balance.
    split = split_budget_on(state.split_lanes, state.lane_split)
    bank = state.current_balance / state.split_lanes if split else state.current_balance
    depth = martingale_chain_depth(bank, state.base_bet, loss_mult)
    if sk in _DOUBLING_KEYS and state.streak_reset_losses and state.max_loss is None and depth:
        warnings.append(
            f"Streak reset with no `max_loss`: {'lane share' if split else 'balance'} "
            f"covers a {depth}-loss chain. "
            f"Loss {depth + 1} in a row ends the session with insufficient balance."
        )
    if sk == "3" and state.max_profit is None:
        warnings.append(
            "Anti-Martingale grows bets on wins with no lock-in. "
            "Consider `maxprofit <amount>` to bank a target."
        )
    if state.current_balance > 0 and state.base_bet > 0.01 * state.current_balance:
        pct = state.base_bet / state.current_balance * 100
        warnings.append(
            f"Base bet is {pct:.1f}% of current balance ({state.base_bet:.8f} of "
            f"{state.current_balance:.8f}) -- small bankrolls drain in ~7 losses at this size."
        )
    if mult > 10:
        warnings.append(
            f"Multiplier {mult}x -> win chance ~{_get_win_chance():.1f}%. "
            "Most sessions at this multiplier end in the red."
        )
    if state.recurring and state.max_profit is None and state.max_bets is None:
        rd = state.recurring_delay or 0.0
        gap = f"delay {rd:.0f}s between cycles" if rd > 0 else "back-to-back (no delay)"
        warnings.append(
            "Recurring restart is on with no max_profit and no max_bets -- "
            f"sessions could restart indefinitely ({gap}). Consider setting at least one cap."
        )
    if sk == _RULES_KEY and state.max_bets is None:
        warnings.append(
            f"Custom rules (Strategy {_RULES_KEY}) with no `max_bets` cap can loop. "
            "Consider setting `max_bets` for safety."
        )
    if state.basebet_pct >= 0.05:
        warnings.append(
            f"basebet_pct = {state.basebet_pct*100:.1f}% of balance -- "
            "the base bet grows quickly on winning streaks."
        )
    if state.proxy and state.proxy.startswith("http://"):
        warnings.append(
            f"proxy scheme is http:// ({mask_proxy(state.proxy)}) -- "
            "credentials + traffic travel unencrypted between you and the proxy. "
            "Prefer https:// or socks5:// when possible."
        )
    return warnings


# ===========================================================
#  RUNTIME TWEAK GRAMMAR  (WolfBet v2.19.0 / v2.30.6)
# ===========================================================
_TWEAK_ALIAS = {
    "delay":       "bet_delay",
    "sdloss":      "streak_delay_loss",
    "sdwin":       "streak_delay_win",
    "sdbets":      "streak_delay_bets",
    "sbloss":      "streakbet_loss",
    "bbpct":       "basebet_pct",
    "maxprofit":   "max_profit",
    "maxloss":     "max_loss",
    "maxbets":     "max_bets",
    "maxwins":     "max_wins",
    "minbalance":  "stop_on_balance",
    "msbets":      "milestone_bets",
    "mswins":      "milestone_wins",
    "mslosses":    "milestone_losses",
    "msprofit":    "milestone_profit",
    "pi":          "profit_increment",
    "pt":          "profit_threshold",
    "piscope":     "profit_increment_scope",
    "pscope":      "profit_increment_scope",
    "basebet":     "base_bet",
    "multiplier":  "multiplier_target",
    "mult":        "multiplier_target",
    "lossmult":    "loss_mult",
    "winmult":     "win_mult",
    "recdelay":    "recurring_delay",
    "recurringdelay": "recurring_delay",
    "srloss":      "streak_reset_losses",
    "srdelay":     "streak_reset_delay",
    "purgedays":   "purge_days",
    "dbmode":      "db_write_mode",
    "dbwritemode": "db_write_mode",
}
_TWEAK_PARAMS = {
    "bet_delay", "basebet_pct",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold", "profit_increment_scope",
    "recurring", "recurring_delay",
    "streak_reset_losses", "streak_reset_delay",
    "base_bet", "multiplier_target", "loss_mult", "win_mult",
    "proxy", "purge_days", "db_write_mode",
    "currency", "key_ref", "proxy_ref",
}
_TWEAK_OFF_VALUES = (None, "", "off", "none", "0", "disable", "false", "no", "n")


def _dice_target_for(multiplier: float, condition: str) -> float:
    """Dice target for a multiplier -- same formula as the wizard's step_bet_config."""
    wc = 99.0 / multiplier
    return round(100.0 - wc, 2) if condition == "above" else round(wc, 2)


def _apply_mutation(changes: dict) -> list:
    """Apply {param: value} to state.* and return status lines.

    Raises ValueError on malformed input. Clamped values add a "⚠" line.
    Stake has no server-side auto-bet, so nothing is cleared: a new base
    bet / multiplier applies to the next bet.
    """
    msgs = []

    def _is_off(v):
        return v is None or (isinstance(v, str) and v.strip().lower() in _TWEAK_OFF_VALUES)

    def _clamped(name: str, raw: float, lo: float, hi: float) -> float:
        out = max(lo, min(hi, raw))
        if out != raw:
            msgs.append(f"⚠ {name} clamped from {raw:g} to {out:g} (allowed: {lo:g}..{hi:g})")
        return out

    def _num(name, v, cast=float):
        number = finite_float(name, v)
        return int(number) if cast is int else number

    if "bet_delay" in changes:
        state.bet_delay = max(0.0, _num("bet_delay", changes["bet_delay"]))
        msgs.append(f"Delay: {state.bet_delay:.3f}s")

    if "basebet_pct" in changes:
        v = changes["basebet_pct"]
        state.basebet_pct = 0.0 if _is_off(v) else _num("basebet_pct", v)
        if state.basebet_pct > 0 and state.current_balance > 0:
            _apply_basebet_pct()
            msgs.append(f"Basebet pct: {state.basebet_pct*100:.4f}% -> base={state.base_bet:.8f}")
        else:
            msgs.append("Basebet pct: off" if not state.basebet_pct
                        else f"Basebet pct: {state.basebet_pct*100:.4f}%")

    for attr, label in [("streak_delay_loss", "Streak delay loss"),
                        ("streak_delay_win",  "Streak delay win"),
                        ("streak_delay_bets", "Streak delay bets"),
                        ("streakbet_loss",    "Streakbet loss")]:
        if attr in changes:
            v = changes[attr]
            _set_config_value(attr, None if _is_off(v) else v)
            n, x = getattr(state, attr)
            msgs.append(f"{label}: every {n} -> {x:.3f}" if n > 0 else f"{label}: off")

    for attr, label, cast in [("max_profit",      "Max profit",  float),
                              ("max_loss",        "Max loss",    float),
                              ("max_bets",        "Max bets",    int),
                              ("max_wins",        "Max wins",    int),
                              ("stop_on_balance", "Min balance", float)]:
        if attr in changes:
            v = changes[attr]
            if _is_off(v):
                setattr(state, attr, None)
                msgs.append(f"{label}: off")
            else:
                setattr(state, attr, _num(attr, v, cast))
                msgs.append(f"{label}: {getattr(state, attr)}")

    for attr, label in [("milestone_bets",   "Milestone bets"),
                        ("milestone_wins",   "Milestone wins"),
                        ("milestone_losses", "Milestone losses")]:
        if attr in changes:
            v = changes[attr]
            setattr(state, attr, 0 if _is_off(v) else _num(attr, v, int))
            msgs.append(f"{label}: {getattr(state, attr) or 'off'}")
    if "milestone_profit" in changes:
        v = changes["milestone_profit"]
        state.milestone_profit = 0.0 if _is_off(v) else _num("milestone_profit", v)
        msgs.append(f"Milestone profit: {state.milestone_profit:.8f}"
                    if state.milestone_profit else "Milestone profit: off")

    if "profit_increment" in changes:
        v = changes["profit_increment"]
        state.profit_increment = None if _is_off(v) else _num("profit_increment", v)
        if state.profit_increment is not None and state.profit_increment <= 0:
            state.profit_increment = None          # v1.15.0 -- 0 / 0.0 means off
        msgs.append(f"Profit increment: {state.profit_increment:.8f}"
                    if state.profit_increment else "Profit increment: off")
    if "profit_threshold" in changes:
        v = changes["profit_threshold"]
        state.profit_threshold = None if _is_off(v) else _num("profit_threshold", v)
        if state.profit_threshold is not None and state.profit_threshold <= 0:
            state.profit_threshold = None          # v1.15.0 -- 0 / 0.0 means off
        if state.profit_threshold:
            # run scope: next milestone after the bumps already made
            done = state.run_bumps if state.profit_increment_scope == "run" else 0
            state.next_profit_milestone = next_milestone(done, state.profit_threshold)
        msgs.append(f"Profit threshold: {state.profit_threshold:.8f}"
                    if state.profit_threshold else "Profit threshold: off")
    if "profit_increment_scope" in changes:
        state.profit_increment_scope = parse_scope(changes["profit_increment_scope"])
        msgs.append(f"Profit increment scope: {state.profit_increment_scope} "
                    "(applies on next start)")

    if "recurring" in changes:
        state.recurring = str(changes["recurring"]).strip().lower() in ("on", "true", "yes", "y", "1")
        msgs.append(f"Recurring: {'on' if state.recurring else 'off'}")

    for attr, label in [("recurring_delay", "Recurring delay"),
                        ("streak_reset_delay", "Streak reset delay")]:
        if attr in changes:
            v = changes[attr]
            if _is_off(v):
                setattr(state, attr, 0.0)
            else:
                try:
                    raw = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{attr} must be a number 0-{RECURRING_DELAY_MAX:g}, got {v!r}")
                setattr(state, attr, _clamped(attr, raw, 0.0, float(RECURRING_DELAY_MAX)))
            val = getattr(state, attr)
            msgs.append(f"{label}: {val:.0f}s" if val > 0 else f"{label}: off")

    if "streak_reset_losses" in changes:
        v = changes["streak_reset_losses"]
        if _is_off(v):
            state.streak_reset_losses = None
        else:
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"streak_reset_losses must be an integer >= 1, got {v!r}")
            if n < 1:
                raise ValueError(f"streak_reset_losses must be an integer >= 1, got {v!r}")
            state.streak_reset_losses = n
        msgs.append(f"Streak reset: after {state.streak_reset_losses} losses"
                    if state.streak_reset_losses else "Streak reset: off")

    if "proxy" in changes:
        try:
            state.proxy = validate_proxy(changes["proxy"]) or ""
        except ValueError as e:
            raise ValueError(f"proxy: {e}")
        _recreate_http()
        msgs.append(f"Proxy: {mask_proxy(state.proxy)}")

    if "currency" in changes:
        cur = str(changes["currency"] or "").strip().lower()
        if cur not in CURRENCIES:
            raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}, got {changes['currency']!r}")
        state.currency = cur
        msgs.append(f"Currency: {cur.upper()} (restart to apply)")

    # Lane accounts: a vault account and a vault proxy, by id; each must exist and be of its kind.
    for ref_key, kind in (("key_ref", "account"), ("proxy_ref", "proxy")):
        if ref_key not in changes:
            continue
        ref = str(changes[ref_key] or "").strip().lower()
        if ref_key == "proxy_ref" and _is_off(ref):
            state.proxy_ref = None
            msgs.append("Proxy from the vault: off (restart to apply)")
            continue
        info = vault_info(VAULT_SERVICE, ref, log=logger)
        if info is None or info.kind != kind:
            raise ValueError(f"no {'key' if kind == 'account' else kind} named {ref} in the vault")
        setattr(state, ref_key, ref)
        msgs.append(f"{'Key' if kind == 'account' else 'Proxy'}: {ref} (restart to apply)")

    if "purge_days" in changes:
        v = changes["purge_days"]
        if isinstance(v, str) and v.strip().lower() in ("off", "none", "false", "disable", "no"):
            state.purge_days = 3
        else:
            try:
                state.purge_days = int(_clamped("purge_days", float(v), 1.0, 30.0))
            except (TypeError, ValueError):
                raise ValueError(f"purge_days must be an integer 1-30, got {v!r}")
        msgs.append(f"Purge days: {state.purge_days}")

    if "db_write_mode" in changes:
        v = str(changes["db_write_mode"]).strip().lower()
        if v not in ("async", "sync"):
            raise ValueError(f"db_write_mode must be 'async' or 'sync', got {v!r}")
        state.db_write_mode = v
        msgs.append(f"DB write mode: {state.db_write_mode}")

    if "base_bet" in changes:
        bet = _num("base_bet", changes["base_bet"])
        if bet < 0:
            raise ValueError(f"base_bet must be >= 0, got {changes['base_bet']!r}")
        if 0 < bet < MIN_BET:
            msgs.append(f"⚠ base_bet raised from {bet:g} to the minimum {MIN_BET:g}")
            bet = MIN_BET
        state.base_bet = state.current_bet = bet
        msgs.append(f"Base bet: {state.base_bet:.8f}")
    if "multiplier_target" in changes:
        m = _num("multiplier", changes["multiplier_target"])
        if m < 1.01:
            raise ValueError(f"multiplier must be at least 1.01, got {changes['multiplier_target']!r}")
        state.multiplier_target = m
        msgs.append(f"Multiplier: {state.multiplier_target:.2f}")
        if state.game == "dice":
            state.dice_target = _dice_target_for(m, state.dice_condition)
            msgs.append(f"Dice target: {state.dice_condition} {state.dice_target}")
    if "loss_mult" in changes:
        state.loss_mult = _num("loss_mult", changes["loss_mult"])
        msgs.append(f"Loss mult: {state.loss_mult:.2f}")
    if "win_mult" in changes:
        state.win_mult = _num("win_mult", changes["win_mult"])
        msgs.append(f"Win mult: {state.win_mult:.2f}")

    return msgs

# ===========================================================
#  API HELPERS
# ===========================================================
def _headers(base: str = None) -> dict:
    b = (base or API_BASE).split("/_api")[0]
    ua = _cf_user_agent or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    h = {
        "Content-Type":     "application/json",
        "Accept":           "*/*",
        "x-access-token":   state.access_token,
        "x-lockdown-token": state.lockdown_token,
        "x-language":       "en",
        "Origin":           b,
        "Referer":          b + f"/casino/games/{state.game}",
        "User-Agent":       ua,
    }
    proxy_key = os.environ.get("STAKE_PROXY_KEY")
    if proxy_key:
        h["X-Proxy-Key"] = proxy_key
    cookie_parts = []
    if _cf_cookie_str:
        cookie_parts.append(_cf_cookie_str)
    if state.cookie:
        cookie_parts.append(state.cookie)
    if cookie_parts:
        h["Cookie"] = "; ".join(cookie_parts)
    return h

_USER_BALANCES_QUERY = """query UserBalances {
  user {
    id
    balances {
      available { amount currency __typename }
      __typename
    }
    __typename
  }
}"""

def _gql_url() -> str:
    return API_BASE.split("/_api/casino")[0] + "/_api/graphql"

def _user_balances_payload() -> dict:
    return {"operationName": "UserBalances", "query": _USER_BALANCES_QUERY, "variables": {}}

def api_get_balances() -> list:
    """Fetch all balances via GraphQL. Returns list of {currency, amount} dicts."""
    try:
        r = _http.post(_gql_url(), headers=_headers(), json=_user_balances_payload(), timeout=15)
        if r.status_code == 200:
            data = r.json()
            user = data.get("data", {}).get("user", {})
            balances = user.get("balances", [])
            return [{"currency": b["available"]["currency"],
                     "amount": float(b["available"]["amount"])}
                    for b in balances if float(b["available"]["amount"]) > 0]
    except Exception as e:
        logger.debug("Balance fetch failed: %s", e)
    return []

def _api_post(url: str, payload: dict, base: str = None) -> Optional[dict]:
    """Make a POST request via curl_cffi (with CF cookies if solved)."""
    r = _http.post(url, headers=_headers(base=base), json=payload, timeout=15)
    if r.status_code != 200:
        body = r.text[:300] if r.text else "(empty)"
        raise ConnectionError(f"HTTP {r.status_code} from {url}: {body}")
    return r.json()

def _cf_reauth(try_all_bases, err):
    """Cloudflare passes 2-6 of api_test_connection (cache, TLS profiles,
    FlareSolverr, Playwright, nodriver), unchanged. Returns (base, None) on
    success or (None, last error). The caller holds _cf_reauth_lock."""
    _try_all_bases = try_all_bases
    # Pass 2: cached CF cookies
    if "403" in str(err) and _cf_cache_load():
        console.print("  [yellow]Using cached CF cookies...[/yellow]")
        base2, err2 = _try_all_bases()
        if base2:
            return base2, None
        err = err2

    # Pass 3: curl_cffi profile rotation
    if "403" in str(err) and _HAS_CFFI:
        for profile in ("chrome120", "chrome116", "chrome110", "firefox", "edge"):
            console.print(f"  [yellow]Trying TLS profile: {profile}...[/yellow]")
            _recreate_http(impersonate=profile)
            base3, err3 = _try_all_bases()
            if base3:
                return base3, None
            if "403" not in str(err3):
                err = err3
                break
            err = err3

    # Pass 4: FlareSolverr / Byparr
    if "403" in str(err) and _flaresolverr_available():
        for domain_base in API_BASES:
            site = domain_base.split("/_api")[0]
            console.print(f"  [yellow]Solving Cloudflare via FlareSolverr for {site}...[/yellow]")
            if _solve_cloudflare(site):
                _recreate_http()  # clear stale CF cookies from session
                base4, err4 = _try_all_bases()
                if base4:
                    return base4, None
                err = err4

    # Pass 5: Playwright (handles Turnstile / managed challenges)
    if "403" in str(err) and _playwright_available():
        for domain_base in API_BASES:
            site = domain_base.split("/_api")[0]
            console.print(f"  [yellow]Solving Cloudflare via Playwright for {site}...[/yellow]")
            if _solve_cf_playwright(site):
                _recreate_http()
                base5, err5 = _try_all_bases()
                if base5:
                    return base5, None
                err = err5

    # Pass 6: nodriver (most aggressive CF bypass -- patched Chrome via CDP)
    if "403" in str(err):
        try:
            import nodriver  # noqa: F401
            for domain_base in API_BASES:
                site = domain_base.split("/_api")[0]
                console.print(f"  [yellow]Solving Cloudflare via nodriver for {site}...[/yellow]")
                if _solve_cf_nodriver(site):
                    _recreate_http()
                    base6, err6 = _try_all_bases()
                    if base6:
                        return base6, None
                    err = err6
        except ImportError:
            console.print("  [dim]nodriver not installed (skip). To enable: pip install nodriver[/]")

    return None, err

def api_test_connection() -> bool:
    """Test auth with a read-only GraphQL query. CF bypass: profile rotation → cache → FlareSolverr."""
    global API_BASE
    query = """query AuthCheck { user { id __typename } }"""
    payload = {"operationName": "AuthCheck", "query": query, "variables": {}}

    def _check(data):
        return (data and isinstance(data.get("data"), dict)
                and data["data"].get("user") is not None)

    def _try_all_bases():
        last_err = None
        for base in API_BASES:
            gql_url = base.split("/_api/casino")[0] + "/_api/graphql"
            try:
                data = _api_post(gql_url, payload, base=base)
                if _check(data):
                    return base, None
                last_err = f"Auth check: unexpected response from {gql_url}"
            except Exception as e:
                last_err = str(e)
        return None, last_err

    # Pass 1: direct
    base, err = _try_all_bases()
    if base:
        API_BASE = base
        return True

    # Passes 2-6 re-authenticate with Cloudflare. v1.13.0: parallel lanes share
    # the cookie cache, so they run one lane at a time; a lane that waited for
    # the lock re-reads the cache in pass 2 and reuses the first lane's cookies.
    if "403" in str(err):
        with _cf_reauth_lock():
            base, err = _cf_reauth(_try_all_bases, err)
        if base:
            API_BASE = base
            return True

    if "403" in str(err):
        raise Exception(
            "Cloudflare hard-banned this server's IP. The IP is the disqualifying signal\n"
            "  ('Just a moment...' never resolves even from a real browser). Free fixes,\n"
            "  in order of how likely they are to work:\n"
            "    1. Cloudflare WARP (free, 30 sec setup):\n"
            "         sudo bash warp-setup.sh\n"
            "         WARP routes via Cloudflare's network which CF often trusts.\n"
            "    2. Cloudflare Worker proxy (free, 5 min setup, 100K req/day):\n"
            "         Deploy cloudflare-worker/proxy.js → re-run with:\n"
            "         python3 stake.py --api-base https://<your-worker>.workers.dev\n"
            "         Workers run on CF's own network -- they bypass IP scoring entirely.\n"
            "    3. Run from your local machine (free, your home IP works):\n"
            "         git clone the repo and run python3 stake.py there.\n"
            "    4. ProtonVPN free tier (free, residential exits in 3 countries):\n"
            "         https://protonvpn.com/free-vpn/  → use the config with WireGuard.\n"
            "  See README for details on each."
        )
    raise Exception(err or "All API domains failed")

def api_place_bet(amount: float) -> Optional[dict]:
    """Place a bet on the current game via REST. Returns parsed result dict or None."""
    if 0 < amount < MIN_BET:
        amount = MIN_BET

    game_info = GAMES[state.game]
    endpoint = game_info["endpoint"]
    resp_key = game_info["response_key"]
    build = game_info["build_payload"]
    parse = game_info["parse_result"]

    saved = state.current_bet
    state.current_bet = amount
    payload = build(state)
    state.current_bet = saved

    url = API_BASE + endpoint
    logger.debug("BET [%s] url=%s payload=%s", state.game, url, json.dumps(payload))
    try:
        t0 = time.perf_counter()
        try:
            data = _api_post(url, payload)
        except ConnectionError:
            _record_api_ms((time.perf_counter() - t0) * 1000)   # HTTP reply, non-200
            raise
        _record_api_ms((time.perf_counter() - t0) * 1000)
        if data is None:
            with state.lock:
                state.last_error = "Empty response (proxy timeout?)"
            return None
        # HTTP-200 error envelope: not a bet (was booked as a 0-amount loss).
        errors = data.get("errors") or data.get("error")
        if errors:
            logger.error("Bet API error: %s", str(errors)[:300])
            with state.lock:
                if "insufficient" in str(errors).lower():
                    state.last_error = "Insufficient balance"
                    state._insufficient_balance = True
                else:
                    state.last_error = f"API error: {str(errors)[:120]}"
            return None
        raw = data.get(resp_key, data)
        if not raw:
            logger.error("Empty response from %s: %s", url, data)
            with state.lock:
                state.last_error = f"Empty response from {endpoint}"
            return None
        result = parse(raw)
        logger.debug("BET OK  id=%s result=%s win=%s",
                     raw.get("id"), result["result_display"], result["is_win"])
        return result
    except ConnectionError as e:
        err = str(e)
        if "429" in err:
            logger.warning("RATE LIMIT 429")
            with state.lock:
                state.last_error = "Rate limit 429 -- backing off..."
        elif "insufficient" in err.lower():
            logger.warning("INSUFFICIENT BALANCE")
            with state.lock:
                state.last_error = "Insufficient balance"
                state._insufficient_balance = True
        else:
            logger.error("API error: %s", err)
            with state.lock:
                state.last_error = err[:120]
    except requests.exceptions.Timeout:
        logger.warning("REQUEST TIMEOUT on bet #%d", state.total_bets + 1)
        with state.lock:
            state.last_error = "Request timeout -- retrying..."
    except Exception as e:
        logger.exception("UNEXPECTED ERROR placing bet: %s", e)
        with state.lock:
            state.last_error = str(e)[:120]
    return None

# ===========================================================
#  STRATEGY ENGINE
# ===========================================================
def compute_next_bet(last_result: str) -> float:
    key  = state.strategy_key
    base = state.base_bet
    cur  = state.current_bet
    lm   = state.loss_mult
    wm   = state.win_mult

    if key == "1":   # Flat
        return base
    elif key == "2": # Martingale
        return (cur * lm) if last_result == "loss" else base
    elif key == "3": # Anti-Martingale
        return (cur * wm) if last_result == "win" else base
    elif key == "4": # D'Alembert
        if last_result == "loss":
            state.dalembert_unit += 1
        elif last_result == "win" and state.dalembert_unit > 0:
            state.dalembert_unit -= 1
        return base * (1 + state.dalembert_unit)
    elif key == "5": # Paroli
        if last_result == "win":
            state.paroli_count += 1
            if state.paroli_count >= 3:
                state.paroli_count = 0
                return base
            return cur * wm
        else:
            state.paroli_count = 0
            return base
    elif key == "6": # Delay Martingale
        if last_result == "win":
            return base
        consec_losses = abs(min(state.current_streak, 0))
        if consec_losses <= state.delay_martin_threshold:
            return base
        return cur * lm
    elif key == "7": # Rule-Based
        return cur
    return base

def should_stop() -> tuple:
    p = state.profit
    b = state.current_balance
    if state.max_profit is not None and p >= state.max_profit:
        return True, f"Profit target reached: {p:+.8f}"
    if state.max_loss is not None and p <= -abs(state.max_loss):
        return True, f"Max loss hit: {p:+.8f}"
    if state.max_bets is not None and state.total_bets >= state.max_bets:
        return True, f"Max bets reached: {state.total_bets}"
    if state.max_wins is not None and state.wins >= state.max_wins:
        return True, f"Max wins reached: {state.wins}"
    if state.stop_on_balance is not None and b <= state.stop_on_balance:
        return True, f"Balance floor hit: {b:.8f}"
    if streak_reset_triggered(state.current_streak, state.last_broken_loss_streak,
                              state.streak_reset_losses, p):
        return True, streak_reset_stop_reason(state.last_broken_loss_streak)
    # v1.13.0 (wolfbet v2.35.0) -- split budget: a lane may only risk its share.
    # Not on the restart whitelist, so the lane stops for good.
    over, reason = lane_budget_exceeded(state.current_bet, lane_equity_now())
    if over:
        return True, reason
    return False, ""

def lane_equity_now() -> Optional[float]:
    """Money this lane may still risk: share + its result so far. None = no split.
    Stake's bet reply carries no balance, so equity comes from lane profit."""
    return lane_equity(state.lane_share, state.lane_run_profit, state.profit)

# ── v1.15.0 -- run-wide profit increment ───────────────────
def _increment_on() -> bool:
    """Increment active only with both values set and > 0."""
    return increment_on(state.profit_increment, state.profit_threshold)


def _run_path(lane: int = None) -> str:
    """This lane's run file (lane 1 keeps the classic path)."""
    return run_path(RUN_PATH, getattr(state, "lane", 1) if lane is None else lane)


def _save_run() -> None:
    if state.run_first_session is None:
        return
    try:
        save_run(_run_path(), {"first_session": state.run_first_session,
                               "started_at": state.run_since,
                               "bumps": state.run_bumps,
                               "base": state.run_base_origin})
    except OSError as e:
        logger.warning("run file save failed: %s", e)


def _run_profit_done_from_db(first_sid: int) -> float:
    """Sum of this lane's session profits since `first_sid` (this one excluded)."""
    conn = _db_conn()
    try:
        return run_profit_done(conn, first_sid, state.session_id, state.lane)
    finally:
        conn.close()


def _run_max_session_id() -> int:
    conn = _db_conn()
    try:
        return db_max_session_id(conn)
    finally:
        conn.close()


def run_profit_total() -> float:
    """Run total: the run's finished sessions plus the live session."""
    return state.run_profit_done + state.profit


def _check_profit_increment() -> None:
    """Bump base_bet when profit reaches the next milestone. Caller holds state.lock.

    Scope "session" measures this session's profit (milestone reset each
    recurring cycle); scope "run" measures the total since the run started and
    keeps each bump in the run file. Stake has no server-side auto-bet, so a
    bumped base bet applies to the next bet.
    """
    if not _increment_on():
        return
    run = state.profit_increment_scope == "run"
    bumped, base, milestone, bumps = check_profit_increment(
        run_profit_total() if run else state.profit,
        state.next_profit_milestone, state.profit_threshold,
        state.profit_increment, state.base_bet, state.run_bumps)
    if not bumped:
        return
    state.base_bet = base
    state.next_profit_milestone = milestone
    if run:
        state.run_bumps = bumps
        _save_run()
    logger.info("%s: base_bet -> %.8f (next milestone at %.8f profit)",
                "RUN PROFIT INCREMENT" if run else "PROFIT INCREMENT",
                state.base_bet, state.next_profit_milestone)


def _init_run() -> None:
    """Restore (or prepare) the run before the first session row starts.

    Scope "run" only. A saved run puts its bumps on top of the loaded base bet
    and loads the profit of its finished sessions. A file pointing past the
    DB's last session (the stats were reset) starts a new run. Off or
    per-session scope removes a leftover run so old bumps never come back.
    """
    state.run_first_session = None
    state.run_bumps = 0
    state.run_profit_done = 0.0
    state.run_base_origin = None
    if state.profit_increment_scope != "run" or not _increment_on():
        _end_run("increment off" if state.profit_increment_scope == "run"
                 else "scope session")
        return
    run = load_run(_run_path())
    if run and run_is_stale(run, _run_max_session_id()):
        logger.warning("RUN file points past the stats DB (reset?) -- starting a new run")
        run = None
    if run:
        state.run_first_session = run["first_session"]
        state.run_since = run["started_at"]
        state.run_bumps = bumps_for_base(run["bumps"], run["base"], state.base_bet)
        dropped = state.run_bumps != run["bumps"]
        if dropped:
            logger.warning("RUN base changed (%.8f -> %.8f) -- dropping %d bump(s), re-anchoring",
                           run["base"], state.base_bet, run["bumps"])
        state.run_base_origin = state.base_bet
        state.run_profit_done = _run_profit_done_from_db(run["first_session"])
        state.base_bet = resume_base_bet(state.base_bet, state.run_bumps,
                                         state.profit_increment)
        state.current_bet = state.base_bet
        if run["base"] is None or dropped:
            _save_run()
    state.next_profit_milestone = (
        rebaseline(state.run_profit_done, state.profit_threshold) if run and dropped
        else next_milestone(state.run_bumps, state.profit_threshold))
    logger.warning("RUN %s  since #%s  bumps=%d  run profit=%+.8f  base_bet=%.8f",
                   "resumed" if run else "new", state.run_first_session or "next",
                   state.run_bumps, state.run_profit_done, state.base_bet)


def _end_run(reason: str) -> None:
    """Drop this lane's run: file removed, run counters cleared."""
    had = state.run_first_session is not None
    try:
        os.remove(_run_path())
        had = True
    except OSError:
        pass
    state.run_first_session = None
    state.run_since = None
    state.run_bumps = 0
    state.run_profit_done = 0.0
    state.run_base_origin = None
    if had:
        logger.warning("RUN ended (%s) -- the next run starts fresh", reason)


def _attach_run(session_id: int) -> None:
    """A new run begins at the first session this bot starts."""
    if (state.profit_increment_scope == "run" and _increment_on()
            and state.run_first_session is None and session_id):
        state.run_first_session = session_id
        state.run_since = datetime.now().isoformat()
        state.run_base_origin = state.base_bet
        _save_run()


def _rebase_run_for_base_change(new_base: float) -> None:
    """Make an operator-set base the one the bot bets. Caller holds state.lock.

    Bumps earned on the previous base are dropped and the milestone re-anchors
    above the run's current profit, so the next bump needs fresh profit. Only
    numbers change: no CF re-auth, no client rebuild.
    """
    state.base_bet = state.current_bet = new_base
    if state.profit_increment_scope == "run" and state.run_first_session is not None:
        state.run_bumps = 0
        state.run_base_origin = new_base
        state.next_profit_milestone = rebaseline(run_profit_total(), state.profit_threshold)
        _save_run()


def cmd_run_reset(lane: int = None) -> int:
    """Delete the run file(s): the next start begins a new run. Returns count removed."""
    n = 0
    for k in ([lane] if lane else lane_file_ids(RUN_PATH)):
        try:
            os.remove(run_path(RUN_PATH, k))
            n += 1
        except OSError:
            continue
    console.print(f"[green]Run reset[/] -- {n} run file(s) removed. "
                  "The next start begins a new run total.")
    return n

def _init_lane_budget() -> None:
    """v1.13.0 (wolfbet v2.35.0) -- fix this lane's share of the balance at bot
    start. The balance is the account's, read via GraphQL UserBalances."""
    state.lane_share = lane_share(state.current_balance, state.split_lanes, state.lane_split)
    if state.lane_share is not None:
        logger.warning("LANE %d/%d  share=%.8f of balance %.8f",
                       state.lane, state.split_lanes, state.lane_share,
                       state.current_balance)

def _apply_basebet_pct() -> bool:
    """Set base bet to balance x basebet_pct. True when it changed (WolfBet v2.16.0)."""
    if state.basebet_pct <= 0 or state.current_balance <= 0:
        return False
    # v1.13.0 (wolfbet v2.36.0) -- bet split divides by the lane count.
    # v1.14.0 -- a split bet that rounds to 0 or is below MIN_BET stays full.
    new_base, note = split_base_bet(
        round(state.current_balance * state.basebet_pct, 8),
        state.split_lanes, state.lane_split, False, MIN_BET)
    if note:
        logger.warning(note)
    if new_base <= 0 or abs(new_base - state.base_bet) < 1e-12:
        return False
    with state.lock:
        state.base_bet = new_base
        state.current_bet = new_base
    logger.info("BASEBET_PCT: base_bet -> %.8f", new_base)
    return True

def _check_milestone(last_bet_state: str) -> Optional[str]:
    """Milestone text when one is crossed on this bet, else None (WolfBet v2.16.0)."""
    if state.milestone_bets and state.total_bets % state.milestone_bets == 0:
        return f"{state.total_bets} bets"
    if (state.milestone_wins and last_bet_state == "win"
            and state.wins % state.milestone_wins == 0):
        return f"{state.wins} wins"
    if (state.milestone_losses and last_bet_state != "win"
            and state.losses % state.milestone_losses == 0):
        return f"{state.losses} losses"
    if state.milestone_profit and state.milestone_profit > 0:
        crossed = int(state.profit / state.milestone_profit) * state.milestone_profit
        if crossed > state._last_profit_milestone:
            state._last_profit_milestone = crossed
            return f"{crossed:.8f} profit"
    return None

def _refresh_balance():
    """Read the real balance once via GraphQL UserBalances; keep the local one on failure.

    Stake returns no balance in the bet reply, so the CLI tracks it locally;
    each new cycle starts from the account's real balance.
    """
    for b in api_get_balances():
        if b["currency"] == state.currency:
            with state.lock:
                state.current_balance = b["amount"]
            return

def _reset_session_for_recurring(delay: float) -> bool:
    """Close-out pause, then open a new sessions row (WolfBet v2.16.0 / v2.28.4).

    The caller has already saved the old row. Uptime freezes at the bet end
    during the pause. The pause is interruptible by [Q] / SIGTERM
    (``state.running`` False); then no new row is opened and False is returned.
    """
    bet_end = time.time()
    with state.lock:
        state.session_bet_ended_at = bet_end
        state.session_pause_started_at = bet_end
        state.session_pause_total = delay
    remaining = delay
    while remaining > 0 and state.running:
        state.status = f"Recurring pause {remaining:.0f}s before next cycle..."
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step
    if not state.running:
        return False
    _refresh_balance()
    with state.lock:
        state.session_bet_ended_at = None
        state.session_pause_started_at = None
        state.start_balance = state.highest_balance = state.lowest_balance = state.current_balance
        # v1.13.0 (wolfbet v2.35.0) -- lane equity carries the lane's result.
        state.lane_run_profit += state.profit
        # v1.15.0 -- the run total keeps the finished session's result.
        state.run_profit_done += state.profit
        state.session_id = _db_start_session()
        _attach_run(state.session_id)
        state.session_start = time.time()
        state.total_bets = state.wins = state.losses = 0
        state.wagered = state.profit = 0.0
        state.current_streak = state.max_win_streak = state.max_loss_streak = 0
        state.last_broken_loss_streak = 0
        state.highest_bet = state.highest_win = state.biggest_loss = 0.0
        state.recent_bets.clear()
        state.profit_history.clear()
        state.profit_history.append(0.0)
        state.current_bet = state.base_bet          # bumped base kept
        state.dalembert_unit = state.paroli_count = 0
        state._last_profit_milestone = 0.0
        # v1.15.0 -- run scope keeps its milestone across sessions.
        if state.profit_threshold and state.profit_increment_scope != "run":
            state.next_profit_milestone = state.profit_threshold
        state.consecutive_errors = 0
        state.backoff_delay = 1.0
        state.last_error = ""
        state.status = f"Recurring restart -- session #{state.session_id}"
    logger.info("Recurring restart: session #%s", state.session_id)
    return True

# ===========================================================
#  BETTING LOOP  (runs in background thread)
# ===========================================================
def _record_bet_outcome(amount_used: float, payout: float, is_win: bool) -> tuple:
    """Fold one bet result into the session totals. Caller holds state.lock.

    Shared by the live loop and the backtest so both count a bet the same
    way. The balance is tracked locally. Returns (bet_state, raw_profit, new_balance).
    """
    bet_state   = "win" if is_win else "loss"
    raw_profit  = payout - amount_used
    new_balance = state.current_balance + raw_profit

    state.total_bets     += 1
    state.wagered        += amount_used
    state.profit         += raw_profit
    state.current_balance = new_balance

    state.current_streak, broken = record_outcome(state.current_streak, bet_state == "win")
    if broken is not None:
        state.last_broken_loss_streak = broken
    if bet_state == "win":
        state.wins += 1
        if raw_profit > state.highest_win:
            state.highest_win = raw_profit
        state.max_win_streak = max(state.max_win_streak, state.current_streak)
    else:
        state.losses += 1
        if amount_used > state.biggest_loss:
            state.biggest_loss = amount_used
        state.max_loss_streak = max(state.max_loss_streak, abs(state.current_streak))

    if amount_used > state.highest_bet:
        state.highest_bet = amount_used
    if new_balance > state.highest_balance:
        state.highest_balance = new_balance
    if new_balance < state.lowest_balance:
        state.lowest_balance = new_balance

    _check_profit_increment()
    return bet_state, raw_profit, new_balance


def _next_bet_after(bet_state: str) -> float:
    """Next bet from the strategy, the streakbet factor and the casino minimum."""
    raw_next  = compute_next_bet(bet_state)
    next_bet  = max(state.base_bet, raw_next)
    sb_factor = streakbet_loss_factor(state.current_streak, state.streakbet_loss)
    if sb_factor != 1.0:
        next_bet = max(state.base_bet, next_bet * sb_factor)
    if 0 < next_bet < MIN_BET:
        next_bet = MIN_BET
    return next_bet


def betting_loop():
    last_result = "none"

    while state.running:
        loop_start = time.monotonic()
        if state.paused:
            state.status = "PAUSED -- press [R] to resume"
            time.sleep(0.4)
            continue

        stop, reason = should_stop()
        if stop:
            if (not state._insufficient_balance
                    and should_restart(reason, state.profit, state.recurring,
                                       state.streak_reset_losses)):
                logger.info("Cycle end (%s) -- restarting", reason)
                _db_save_session()
                if _session_boundary(final=False):
                    state.running = False
                    state.status = f"STOPPED: {reason}"
                    break
                if _reset_session_for_recurring(
                        restart_delay_for(reason, state.recurring_delay, state.streak_reset_delay)):
                    last_result = "none"
                    continue
                break
            state.running = False
            state.status  = f"STOPPED: {reason}"
            _db_save_session()
            _session_boundary(final=True)
            break

        sd = streak_delay_for(state.current_streak, state.total_bets,
                              state.streak_delay_loss, state.streak_delay_win,
                              state.streak_delay_bets)
        if sd > 0:
            state.status = f"Streak delay {sd:.2f}s (streak {state.current_streak})"
            time.sleep(sd)

        if state.basebet_pct > 0 and state.total_bets > 0 and state.total_bets % 10 == 0:
            _apply_basebet_pct()

        if state.bet_delay > 0:
            time.sleep(state.bet_delay)

        state.status = f"Placing {state.game} bet #{state.total_bets + 1}..."
        result = api_place_bet(state.current_bet)

        if result is None:
            if state._insufficient_balance:
                state.running = False
                state.status = "STOPPED: Insufficient balance"
                _db_save_session()
                break
            with state.lock:
                state.consecutive_errors += 1
                state.backoff_delay = min(
                    state.backoff_delay * 2,
                    state.max_backoff,
                )
            time.sleep(state.backoff_delay)
            continue

        amount_used    = result["amount"]
        payout         = result["payout"]
        is_win         = result["is_win"]
        result_display = result["result_display"]
        result_value   = result["result_value"]

        with state.lock:
            state.consecutive_errors = 0
            state.backoff_delay      = 1.0

            bet_state, raw_profit, new_balance = _record_bet_outcome(amount_used, payout, is_win)

            if state.total_bets % 5 == 0:
                state.profit_history.append(state.profit)

            # game-specific display info
            game_label = GAMES[state.game]["label"]
            extra_info = ""
            if state.game == "dice":
                extra_info = f" | {state.dice_condition} {state.dice_target}"

            state.recent_bets.append({
                "n":     state.total_bets,
                "time":  datetime.now().isoformat(),
                "amt":   amount_used,
                "roll":  result_display,
                "state": bet_state,
                "pnl":   raw_profit,
                "bal":   new_balance,
            })

            # bets-per-second / bets-per-minute
            now = time.time()
            elapsed = now - state.session_start
            if elapsed > 0:
                state.bets_per_second = state.total_bets / elapsed
                state.bets_per_minute = state.bets_per_second * 60

            sec_key = int(now)
            min_key = int(now) // 60
            if sec_key != state._current_sec:
                if state._current_sec > 0 and state._bets_this_sec > 0:
                    if state._bets_this_sec > state.peak_bps:
                        state.peak_bps = state._bets_this_sec
                    if state._bets_this_sec < state.low_bps:
                        state.low_bps = state._bets_this_sec
                state._current_sec = sec_key
                state._bets_this_sec = 1
            else:
                state._bets_this_sec += 1

            if min_key != state._current_min:
                if state._current_min > 0 and state._bets_this_min > 0:
                    if state._bets_this_min > state.peak_bpm:
                        state.peak_bpm = state._bets_this_min
                    if state._bets_this_min < state.low_bpm:
                        state.low_bpm = state._bets_this_min
                state._current_min = min_key
                state._bets_this_min = 1
            else:
                state._bets_this_min += 1

            sign  = "+" if raw_profit >= 0 else ""
            emoji = "W" if bet_state == "win" else "L"
            state.status = (
                f"{emoji} {'WIN' if bet_state=='win' else 'LOSS'} | "
                f"Roll: {result_display}{extra_info} | "
                f"Bet: {amount_used:.8f} | "
                f"P/L: {sign}{raw_profit:.8f} | "
                f"Bal: {new_balance:.8f}"
            )
            state.last_error = ""

            ms = _check_milestone(bet_state)
            if ms:
                state.status = f"Milestone: {ms}"
                logger.info("MILESTONE: %s", ms)

        _db_save_bet_dispatch(result, raw_profit, new_balance)

        # Rule-Based: apply custom rules after bet
        if state.strategy_key == "7" and state.custom_rules:
            apply_rules(bet_state)

        # no cap: the API rejects a bet the balance cannot cover
        state.current_bet = _next_bet_after(bet_state)
        last_result = bet_state

        # cycle-time metric (WolfBet v2.30.4): one full loop iteration
        _record_cycle_ms((time.monotonic() - loop_start) * 1000)

# ===========================================================
#  DATABASE I/O
# ===========================================================
def _db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    # v1.8.0 -- mirrors wolfbet v2.27.0. WAL-safe.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA cache_size = -32000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn

def _db_start_session() -> int:
    conn = _db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions
            (started_at, currency, game, strategy, base_bet, multiplier, start_balance, lane)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        state.currency, state.game, state.strategy,
        state.base_bet, state.multiplier_target, state.start_balance,
        state.lane,
    ))
    conn.commit()
    sid = c.lastrowid
    conn.close()
    return sid

_BET_INSERT_SQL = (
    "INSERT INTO bets "
    "(session_id, timestamp, game, amount, multiplier_target, "
    " result_value, result_display, state, profit, balance_after) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)"
)

def _make_bet_row(result: dict, profit: float, balance: float) -> tuple:
    """The 10-tuple both the sync and the async INSERT use."""
    return (
        state.session_id,
        datetime.now().isoformat(),
        state.game,
        result["amount"],
        state.multiplier_target,
        result["result_value"],
        result["result_display"],
        "win" if result["is_win"] else "loss",
        profit,
        balance,
    )

def _db_save_bet(result: dict, profit: float, balance: float, row: tuple = None):
    try:
        conn = _db_conn()
        conn.execute(_BET_INSERT_SQL, row or _make_bet_row(result, profit, balance))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_bet failed: %s", e)

# -- Async bet writer (WolfBet v2.27.0): one batched commit off the hot path --
_bet_write_queue: queue.Queue = queue.Queue(maxsize=1000)
_bet_writer_thread: Optional[threading.Thread] = None
_bet_writer_stop = threading.Event()
_bet_writer_stats = {
    "queued": 0,
    "flushed": 0,
    "batches": 0,
    "fallback_sync": 0,
    "queue_high_watermark": 0,
}
_BET_WRITER_BATCH_SIZE   = 32
_BET_WRITER_BATCH_WINDOW = 0.1   # seconds

def _bet_writer_worker():
    """Drain _bet_write_queue in batches of up to 32 within 0.1 s; one commit per batch."""
    try:
        conn = _db_conn()
    except Exception as e:
        logger.error("bet writer failed to open DB (%s) -- bets fall back to sync", e)
        return
    while not (_bet_writer_stop.is_set() and _bet_write_queue.empty()):
        batch = []
        deadline = time.monotonic() + _BET_WRITER_BATCH_WINDOW
        while len(batch) < _BET_WRITER_BATCH_SIZE:
            timeout = max(0.0, deadline - time.monotonic())
            if timeout == 0:
                break
            try:
                batch.append(_bet_write_queue.get(timeout=timeout))
            except queue.Empty:
                break
        if not batch:
            continue
        try:
            conn.executemany(_BET_INSERT_SQL, batch)
            conn.commit()
            _bet_writer_stats["batches"] += 1
            _bet_writer_stats["flushed"] += len(batch)
        except Exception as e:
            logger.warning("bet writer batch failed (%d rows dropped): %s", len(batch), e)
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn = _db_conn()
            except Exception as reopen_err:
                logger.error("bet writer reopen failed: %s -- exiting worker", reopen_err)
                return
    try:
        conn.close()
    except Exception:
        pass

def _start_bet_writer():
    """Spawn the writer thread if not already running. Idempotent."""
    global _bet_writer_thread
    if _bet_writer_thread is not None and _bet_writer_thread.is_alive():
        return
    _bet_writer_stop.clear()
    _bet_writer_thread = threading.Thread(target=_bet_writer_worker, daemon=True, name="BetWriter")
    _bet_writer_thread.start()

def _stop_bet_writer(timeout: float = 5.0):
    """Let the writer drain the queue, then join (bounded wait)."""
    global _bet_writer_thread
    _bet_writer_stop.set()
    if _bet_writer_thread is not None and _bet_writer_thread.is_alive():
        _bet_writer_thread.join(timeout=timeout)
        if _bet_writer_thread.is_alive():
            logger.warning("bet writer did not drain within %.1fs; %d rows may be lost",
                           timeout, _bet_write_queue.qsize())
    _bet_writer_thread = None

def _db_save_bet_async(result: dict, profit: float, balance: float):
    """Enqueue a bet row; a full queue falls back to a sync write so no bet is lost."""
    row = _make_bet_row(result, profit, balance)
    try:
        _bet_write_queue.put_nowait(row)
        _bet_writer_stats["queued"] += 1
        qs = _bet_write_queue.qsize()
        if qs > _bet_writer_stats["queue_high_watermark"]:
            _bet_writer_stats["queue_high_watermark"] = qs
    except queue.Full:
        _bet_writer_stats["fallback_sync"] += 1
        _db_save_bet(result, profit, balance, row=row)

def _db_save_bet_dispatch(result: dict, profit: float, balance: float):
    """Async writer when db_write_mode is async and the thread runs; else sync."""
    if state.db_write_mode == "async" and _bet_writer_thread is not None:
        _db_save_bet_async(result, profit, balance)
    else:
        _db_save_bet(result, profit, balance)

def _db_save_session():
    try:
        conn = _db_conn()
        lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
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
        """, (
            datetime.fromtimestamp(state.session_bet_ended_at or time.time()).isoformat(),
            state.total_bets, state.wins, state.losses,
            state.profit, state.wagered, state.current_balance,
            state.max_win_streak, state.max_loss_streak,
            state.highest_balance, lo_bal,
            state.highest_win, state.biggest_loss,
            min(state.bets_per_second * 60, 999) if state.bets_per_second else state.bets_per_minute,
            state.bets_per_second,
            state.peak_bps,
            state.low_bps if state.low_bps != float("inf") else 0,
            state.peak_bpm,
            state.low_bpm if state.low_bpm != float("inf") else 0,
            state.session_id,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_session failed: %s", e)

def _db_get_history(limit: int = 12):
    conn = _db_conn()
    rows = conn.execute("""
        SELECT id, started_at, ended_at, currency, game, strategy,
               total_bets, profit, wagered, max_win_streak, max_loss_streak
        FROM sessions ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows

# ===========================================================
#  STATE FILE
# ===========================================================
def _save_state_file():
    try:
        data = {
            "pid": os.getpid(),
            "session_id": state.session_id,
            "running": state.running,
            "paused": state.paused,
            "currency": state.currency,
            "game": state.game,
            "strategy": state.strategy,
            "multiplier_target": state.multiplier_target,
            "dice_target": state.dice_target,
            "dice_condition": state.dice_condition,
            "base_bet": state.base_bet,
            "current_bet": state.current_bet,
            "total_bets": state.total_bets,
            "wins": state.wins,
            "losses": state.losses,
            "profit": state.profit,
            "wagered": state.wagered,
            "current_balance": state.current_balance,
            "start_balance": state.start_balance,
            "current_streak": state.current_streak,
            "max_win_streak": state.max_win_streak,
            "max_loss_streak": state.max_loss_streak,
            "highest_bet": state.highest_bet,
            "highest_win": state.highest_win,
            "biggest_loss": state.biggest_loss,
            "bets_per_minute": state.bets_per_minute,
            "bets_per_second": state.bets_per_second,
            "peak_bps": state.peak_bps,
            "low_bps": state.low_bps if state.low_bps != float("inf") else 0,
            "peak_bpm": state.peak_bpm,
            "low_bpm": state.low_bpm if state.low_bpm != float("inf") else 0,
            "highest_balance": state.highest_balance,
            "lowest_balance": state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance,
            "bet_delay": state.bet_delay,
            "loss_mult": state.loss_mult,
            "win_mult": state.win_mult,
            "strategy_key": state.strategy_key,
            "max_profit": state.max_profit,
            "max_loss": state.max_loss,
            "max_bets": state.max_bets,
            "max_wins": state.max_wins,
            "stop_on_balance": state.stop_on_balance,
            "profit_increment": state.profit_increment,
            "profit_threshold": state.profit_threshold,
            # v1.15.0 -- run-wide profit increment
            "profit_increment_scope": state.profit_increment_scope,
            "run_profit_total": run_profit_total(),
            "run_first_session": state.run_first_session,
            "run_bumps": state.run_bumps,
            "rule_count": len(state.custom_rules) if state.strategy_key == "7" else 0,
            "rule_descriptions": [r.description for r in state.custom_rules] if state.strategy_key == "7" else [],
            "next_profit_milestone": state.next_profit_milestone,
            "recurring": state.recurring,
            "recurring_delay_sec": int(state.recurring_delay),
            "streak_reset_losses": state.streak_reset_losses,
            "streak_reset_delay_sec": int(state.streak_reset_delay),
            "streak_delay_loss": _config_value("streak_delay_loss"),
            "streak_delay_win": _config_value("streak_delay_win"),
            "streak_delay_bets": _config_value("streak_delay_bets"),
            "streakbet_loss": _config_value("streakbet_loss"),
            "basebet_pct": state.basebet_pct or None,
            "milestone_bets": state.milestone_bets or None,
            "milestone_wins": state.milestone_wins or None,
            "milestone_losses": state.milestone_losses or None,
            "milestone_profit": state.milestone_profit or None,
            # v1.12.0 -- observability (WolfBet v2.17.0 / v2.30.3-5)
            "api_ms":              state._last_api_ms,
            "api_avg_ms":          _compute_api_avg(),
            "api_peak_ms":         state.peak_api_ms,
            "api_p50_ms":          _compute_api_p50(),
            "api_p99_ms":          _compute_api_p99(),
            "on_target_pct":       _compute_on_target_pct(),
            "cycle_ms":            state._cycle_ms,
            "overhead_ms":         state._cycle_overhead_ms,
            "cycle_p50_ms":        _compute_cycle_p50(),
            "cycle_p99_ms":        _compute_cycle_p99(),
            "cycle_mean_ms":       _compute_cycle_mean(),
            "cycle_sample_count":  len(state._cycle_samples),
            "bets_per_second_recent": _compute_recent_bps(),
            "efficiency_pct":      _compute_efficiency_pct(),
            "theoretical_max_bps": _compute_theoretical_max_bps(),
            "db_write_mode":       state.db_write_mode,
            "db_writer_stats":     dict(_bet_writer_stats),
            "db_writer_queue":     _bet_write_queue.qsize(),
            "purge_days":          state.purge_days,
            # v1.13.0 (wolfbet v2.35.0 / v2.36.0) -- parallel lanes
            "lane":                state.lane,
            "lanes_count":         state.lanes_count,
            "pending_restart":     state.pending_restart,
            "code_version":        VERSION,
            "lane_share":          state.lane_share,
            "lane_split":          state.lane_split,
            "lane_equity":         lane_equity_now(),
            **_uptime_fields(),
            "status": state.status,
            "last_error": state.last_error,
            "updated_at": datetime.now().isoformat(),
        }
        fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _uptime_fields() -> dict:
    """Uptime freezes at the bet end during a cycle pause (WolfBet v2.28.4)."""
    now = time.time()
    return {
        "uptime_sec": int((state.session_bet_ended_at or now) - state.session_start),
        "run_uptime_sec": int(now - state.run_started_at),
        "pause_remaining_sec": (
            max(0, int(state.session_pause_total - (now - state.session_pause_started_at)))
            if state.session_pause_started_at else 0),
        "pause_total_sec": int(state.session_pause_total),
    }

def _cleanup_state_file():
    for p in (STATE_PATH, PID_PATH):
        try:
            os.remove(p)
        except OSError:
            pass

# ===========================================================
#  TUI -- BORDERLESS ULTRA-COMPACT DASHBOARD
# ===========================================================
SPARKLINE = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

def _sparkline(values) -> str:
    if not values or len(values) < 2:
        return "--"
    mn, mx = min(values), max(values)
    span   = mx - mn or 1
    return "".join(SPARKLINE[int((v - mn) / span * 7)] for v in values)

_RST  = "\033[0m"
_BOLD = "\033[1m"
_DIM  = "\033[2m"

_COLORS = {
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "white": "\033[37m",
    "bg_blue": "\033[44m", "bg_magenta": "\033[45m",
    "bold_red": "\033[1;31m", "bold_green": "\033[1;32m",
    "bold_yellow": "\033[1;33m", "bold_cyan": "\033[1;36m",
    "bold_white": "\033[1;37m",
    "dim_blue": "\033[2;34m", "dim_magenta": "\033[2;35m", "dim": "\033[2m",
}
_CLR_EOL = "\033[K"

def _a(color: str, text: str) -> str:
    code = _COLORS.get(color, "")
    return f"{code}{text}{_RST}" if code else text

def _fmt_speed() -> str:
    pbps = int(state.peak_bps) if state.peak_bps > 0 else 0
    lbps = int(state.low_bps) if state.low_bps != float("inf") else 0
    pbpm = int(state.peak_bpm) if state.peak_bpm > 0 else 0
    lbpm = int(state.low_bpm) if state.low_bpm != float("inf") else 0
    return f"{lbps}-{pbps}/s {lbpm}-{pbpm}/m"

def _game_info_str() -> str:
    """Game-specific info for dashboard header."""
    wc = _get_win_chance()
    if state.game == "dice":
        return f"Dice {state.dice_condition} {state.dice_target}  {state.multiplier_target}x  {wc:.1f}%"
    else:
        return f"Limbo {state.multiplier_target}x target  {wc:.1f}% chance"

def _pct_color(pct: float) -> str:
    if pct >= 95:
        return "bold_red"
    if pct >= 80:
        return "yellow"
    return "green"


def _build_config_progress_row(d: dict) -> str:
    """Live config-progress summary row (v1.8.0)."""
    segments = []

    profit_threshold = d.get("profit_threshold")
    profit_increment = d.get("profit_increment")
    if profit_threshold and profit_increment is not None:
        # Per-window progress toward the next bump (WolfBet v2.30.1 / v2.30.7).
        threshold    = float(profit_threshold)
        next_ms      = float(d.get("next_profit_milestone") or threshold)
        window_start = max(0.0, next_ms - threshold)
        profit       = float(d.get("profit", 0.0) or 0.0)
        # v1.15.0 -- run scope measures the total since the run started.
        run_scope    = d.get("profit_increment_scope") == "run"
        if run_scope:
            profit = float(d.get("run_profit_total", profit) or 0.0)
        progress     = max(0.0, profit - window_start)
        pct = min(100.0, progress / threshold * 100) if threshold > 0 else 0.0
        segments.append(
            f"{_a('dim','RunMs' if run_scope else 'Ms')} "
            f"{_a(_pct_color(pct), f'+{progress:.8f}/{threshold:.8f} ({pct:.0f}%)')}"
        )

    max_profit = d.get("max_profit")
    if max_profit is not None:
        profit = float(d.get("profit", 0.0) or 0.0)
        pct = max(0.0, min(100.0, (profit / float(max_profit) * 100) if max_profit else 0.0))
        segments.append(
            f"{_a('dim','MaxP')} "
            f"{_a(_pct_color(pct), f'+{max(0.0, profit):.8f}/{float(max_profit):.8f} ({pct:.0f}%)')}"
        )

    max_loss = d.get("max_loss")
    if max_loss is not None:
        profit = float(d.get("profit", 0.0) or 0.0)
        loss_target = abs(float(max_loss))
        loss_now = abs(profit) if profit < 0 else 0.0
        pct = max(0.0, min(100.0, (loss_now / loss_target * 100) if loss_target > 0 else 0.0))
        color = _pct_color(pct) if profit < 0 else "dim"
        segments.append(
            f"{_a('dim','MaxL')} "
            f"{_a(color, f'-{loss_now:.8f}/-{loss_target:.8f} ({pct:.0f}%)')}"
        )

    max_bets = d.get("max_bets")
    if max_bets is not None:
        total = int(d.get("total_bets", 0) or 0)
        pct = max(0.0, min(100.0, (total / int(max_bets) * 100) if max_bets else 0.0))
        segments.append(
            f"{_a('dim','MaxB')} "
            f"{_a(_pct_color(pct), f'{total}/{int(max_bets)} ({pct:.0f}%)')}"
        )

    max_wins = d.get("max_wins")
    if max_wins is not None:
        wins = int(d.get("wins", 0) or 0)
        pct = max(0.0, min(100.0, (wins / int(max_wins) * 100) if max_wins else 0.0))
        segments.append(
            f"{_a('dim','MaxW')} "
            f"{_a(_pct_color(pct), f'{wins}/{int(max_wins)} ({pct:.0f}%)')}"
        )

    floor = d.get("stop_on_balance")
    if floor is not None:
        bal = float(d.get("current_balance", 0.0) or 0.0)
        color = "yellow" if bal <= float(floor) * 1.05 else "dim"
        segments.append(
            f"{_a('dim','MinBal')} {_a(color, f'{bal:.8f}/{float(floor):.8f}')}"
        )

    if d.get("recurring"):
        delay = int(d.get("recurring_delay_sec", 0) or 0)
        segments.append(f"{_a('dim','Rec')} {_a('cyan', f'on({delay}s)')}")

    if d.get("streak_reset_losses"):
        n = int(d.get("streak_reset_losses") or 0)
        sr_delay = int(d.get("streak_reset_delay_sec", 0) or 0)
        segments.append(f"{_a('dim','SR')} {_a('cyan', f'{n}L→{sr_delay}s')}")

    if not segments:
        return f" {_a('dim','CFG')}  {_a('dim','─')}  {_a('dim','no goals set')}"
    return " " + _a("dim", "CFG") + "  " + "  ".join(segments)


def _pause_label(d: dict) -> str:
    """Cycle-pause header text: remaining / total."""
    left = int(d.get("pause_remaining_sec", 0) or 0)
    total = int(d.get("pause_total_sec") or d.get("recurring_delay_sec", 0) or 0)
    return f"◐ PAUSED {timedelta(seconds=left)}/{timedelta(seconds=total)}"


def _state_dict_from_botstate() -> dict:
    """Build a status-dict view of `state` for _build_config_progress_row."""
    return {
        "profit": state.profit,
        "profit_threshold": getattr(state, "profit_threshold", None),
        "profit_increment": getattr(state, "profit_increment", None),
        "profit_increment_scope": getattr(state, "profit_increment_scope", "session"),
        "run_profit_total": run_profit_total(),
        "next_profit_milestone": state.next_profit_milestone,
        "max_profit": getattr(state, "max_profit", None),
        "max_loss": getattr(state, "max_loss", None),
        "max_bets": getattr(state, "max_bets", None),
        "max_wins": getattr(state, "max_wins", None),
        "stop_on_balance": getattr(state, "stop_on_balance", None),
        "recurring": state.recurring,
        "recurring_delay_sec": int(state.recurring_delay),
        "streak_reset_losses": state.streak_reset_losses,
        "streak_reset_delay_sec": int(state.streak_reset_delay),
        "total_bets": state.total_bets,
        "wins": state.wins,
        "current_balance": state.current_balance,
    }


def build_dashboard_screen() -> str:
    try:
        term_size = os.get_terminal_size()
        term_h = term_size.lines
        term_w = term_size.columns
    except OSError:
        term_h = 24
        term_w = 80

    lines = []

    def add(s: str):
        lines.append(f"{s}{_CLR_EOL}")

    def add_blank():
        lines.append(_CLR_EOL)

    # -- Row 1: Header --
    up = _uptime_fields()
    uptime = str(timedelta(seconds=up["uptime_sec"]))
    if state.running and up["pause_remaining_sec"] > 0:
        st_ind = _a("bold_cyan", _pause_label(up))
    elif state.running and not state.paused:
        st_ind = _a("bold_green", "* LIVE")
    elif state.paused:
        st_ind = _a("bold_yellow", "* PAUSED")
    else:
        st_ind = _a("bold_red", "* STOPPED")

    game_info = _game_info_str()
    run_seg = f"  Run {timedelta(seconds=up['run_uptime_sec'])}"
    add(f"{_a('dim_magenta', '--- ')}{_a('bold_cyan', f'Stake v{VERSION}')}"
        f"{_a('white', f'  #{state.session_id}  {uptime}{run_seg}')}  {st_ind} "
        f"{_a('dim', f'  {game_info}')}"
        f"{_a('dim_magenta', ' ' + '-' * max(0, term_w - 80))}")

    # -- Row 2: Balance bar --
    p = state.profit
    ps = "+" if p >= 0 else ""
    pc = "bold_green" if p >= 0 else "bold_red"
    wr = (state.wins / state.total_bets * 100) if state.total_bets else 0
    wr_c = "bold_green" if wr >= 50 else "bold_red"

    add(f"{_COLORS['bg_magenta']}{_BOLD}{_COLORS['white']} BAL {_RST} "
        f"{_a('bold_white', f'{state.current_balance:.8f} {state.currency.upper()}')}"
        f"  {_a('dim', 'PnL')} {_a(pc, f'{ps}{p:.8f}')}"
        f"  {_a('dim', 'WAG')} {_a('yellow', f'{state.wagered:.8f}')}"
        f"  {_a('dim', 'WR')} {_a(wr_c, f'{wr:.1f}%')}")

    # -- Row 3: Stats --
    cs = state.current_streak
    if cs > 0:
        sk = _a("green", f"W+{cs}")
    elif cs < 0:
        sk = _a("red", f"L{cs}")
    else:
        sk = _a("dim", "--")

    add(f" {_a('dim', 'Bets')} {_a('bold_white', str(state.total_bets))}"
        f"  {_a('green', 'W')} {_a('bold_green', str(state.wins))}"
        f"  {_a('red', 'L')} {_a('bold_red', str(state.losses))}"
        f"  {_a('dim', 'Str')} {sk}"
        f"  {_a('dim', f'Best W+{state.max_win_streak}/L-{state.max_loss_streak}')}")

    # -- Row 4: Bet / Strategy --
    s4 = (f" {_a('dim', 'Bet')} {_a('yellow', f'{state.current_bet:.8f}')}")
    if state.profit_increment and state.profit_threshold:
        s4 += f"  {_a('dim', 'Base')} {_a('bold_green', f'{state.base_bet:.8f}')}"
    s4 += (f"  {_a('dim', 'Hi')} {_a('white', f'{state.highest_bet:.8f}')}"
           f"  {_a('cyan', state.strategy)}"
           f"  {_a('dim', f'{state.multiplier_target}x')}"
           f"  {_a('dim', 'BPS')} {_a('magenta', f'{state.bets_per_second:.1f}')}"
           f"  {_a('dim', 'BPM')} {_a('magenta', f'{state.bets_per_minute:.0f}')}"
           f"  {_a('dim', 'Speed')} {_a('magenta', _fmt_speed())}")
    add(s4)

    # -- Row 5: Extremes --
    hi_bal = state.highest_balance if state.highest_balance > 0 else state.current_balance
    lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
    avg_pnl = state.profit / state.total_bets if state.total_bets else 0.0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    bal_change = state.current_balance - state.start_balance
    bc_c = "bold_green" if bal_change >= 0 else "bold_red"
    bc_s = "+" if bal_change >= 0 else ""

    s5 = (f" {_a('dim', 'Peak')} {_a('green', f'{hi_bal:.8f}')}"
          f"  {_a('dim', 'Low')} {_a('red', f'{lo_bal:.8f}')}"
          f"  {_a('dim', 'BestW')} {_a('green', f'+{state.highest_win:.8f}')}"
          f"  {_a('dim', 'WorstL')} {_a('red', f'-{state.biggest_loss:.8f}')}"
          f"  {_a('dim', 'Avg')} {_a(avg_c, f'{avg_s}{avg_pnl:.8f}')}"
          f"  {_a('dim', 'Bal')} {_a(bc_c, f'{bc_s}{bal_change:.8f}')}")
    add(s5)

    # -- Row 5b (v1.12.0): API timing, Efficiency, Cycle (WolfBet v2.17.0 / v2.30.3) --
    # No rate-limit row: Stake sends no rate-limit headers.
    api_p50 = _compute_api_p50()
    add(f" {_a('dim', 'API')} last {_a('cyan', f'{state._last_api_ms:.0f}ms')}"
        f"  p50 {_a('cyan', f'{api_p50:.0f}ms')}"
        f"  p99 {_a('cyan', f'{_compute_api_p99():.0f}ms')}"
        f"  avg {_a('cyan', f'{_compute_api_avg():.0f}ms')}"
        f"  peak {_a('cyan', f'{state.peak_api_ms:.0f}ms')}")
    if state.bet_delay > 0:
        ot = _compute_on_target_pct()
        ot_str = f"{ot:.0f}%" if ot is not None else "n/a"
        add(f" {_a('dim', 'API stable')} {_a('cyan', f'{api_p50:.0f}ms')} median"
            f"  · {_a('cyan', ot_str)} on-target  (API <= bet delay)")
    tmax = _compute_theoretical_max_bps()
    eff = _compute_efficiency_pct()
    eff_c = 'green' if eff >= 90 else 'yellow'
    eff_note = (f"(max {_a('cyan', f'{tmax:.2f}')} bps @ delay={state.bet_delay:.3f}s)"
                if state.bet_delay > 0 else "(raw throughput, no delay)")
    add(f" {_a('dim', 'Efficiency')} {_a(eff_c, f'{eff:.0f}%')}  {eff_note}"
        f"  {_a('dim', 'Cycle')} {_a('magenta', f'{state._cycle_ms:.0f}ms')}/bet"
        f"  (overhead {_a('cyan', f'{state._cycle_overhead_ms:.0f}ms')})"
        f"  -> {_a('magenta', f'{tmax:.2f}')} bps")

    # -- Row 6: Sparkline --
    spark = _sparkline(list(state.profit_history))
    sp_c = "green" if p >= 0 else "red"
    add(f" {_a('dim', 'P/L')} {_a(sp_c, spark)}")

    # -- Row 6b (v1.8.0): Config-progress --
    add(_build_config_progress_row(_state_dict_from_botstate()))

    # -- Row 7: Separator --
    add(_a("dim_magenta", "-" * term_w))

    # -- Row 8: Bets table header --
    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Amount':>14}  {'Roll':>10}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    # -- Rows 9..N-2: Recent bet rows (what is left above the 2 footer rows) --
    bets_avail = max(0, term_h - len(lines) - 2)
    recent = list(state.recent_bets)[-bets_avail:] if bets_avail > 0 else []

    for b in reversed(recent):
        bwl_c = "bold_green" if b["state"] == "win" else "bold_red"
        bwl_t = " W " if b["state"] == "win" else " L "
        bpnl_c = "green" if b["pnl"] >= 0 else "red"
        bsign = "+" if b["pnl"] >= 0 else ""
        bn = b["n"]
        btm = b["time"]
        bamt = b["amt"]
        broll = b["roll"]
        bpnl = b["pnl"]
        bbal = b["bal"]
        add(f"{_a('dim', f'{bn:>6}')}"
            f"  {_a('dim', f'{btm:<9}')}"
            f" {_a('white', f'{bamt:>14.8f}')}"
            f"  {_a('white', f'{broll:>10}')}"
            f"  {_a(bwl_c, bwl_t)}"
            f"  {_a(bpnl_c, f'{bsign}{bpnl:>14.8f}')}"
            f"  {_a('white', f'{bbal:>14.8f}')}")

    for _ in range(bets_avail - len(recent)):
        add_blank()

    # -- Row N-1: Bottom separator --
    add(_a("dim_magenta", "-" * term_w))

    # -- Row N: Status + controls --
    status_txt = state.status[:60]
    keys = "[P]ause [R]esume [Q]uit [H]istory [T]weak [E]xport"
    add(f" {_a('white', status_txt)}  {_a('dim', keys)}")

    return "\r\n".join(lines)

# ===========================================================
#  HISTORY SCREEN
# ===========================================================
def show_history():
    was_paused  = state.paused
    state.paused = True
    time.sleep(0.15)

    rows = _db_get_history(15)
    console.clear()
    console.print(Rule("[bold cyan]Session History[/]"))

    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("ID",       width=5,  justify="center")
    t.add_column("Started",  width=17)
    t.add_column("Ended",    width=17)
    t.add_column("Cur",      width=6,  justify="center")
    t.add_column("Game",     width=6,  justify="center")
    t.add_column("Strategy", width=18)
    t.add_column("Bets",     width=7,  justify="right")
    t.add_column("Profit",   width=16, justify="right")
    t.add_column("Wagered",  width=16, justify="right")
    t.add_column("W/L Str",  width=10, justify="center")

    for row in rows:
        sid, started, ended, cur, game, strat, bets, profit, wagered, mws, mls = row
        pc  = "green" if (profit or 0) >= 0 else "red"
        ps  = "+" if (profit or 0) >= 0 else ""
        t.add_row(
            str(sid),
            (started or "").replace("T", " "),
            (ended   or "--").replace("T", " "),
            (cur or "").upper(),
            (game or "limbo"),
            strat or "--",
            str(bets or 0),
            Text(f"{ps}{profit:.8f}" if profit else "0.00000000", style=pc),
            f"{wagered:.8f}" if wagered else "--",
            f"W{mws or 0}/L{mls or 0}",
        )

    console.print(t)
    console.print("\n[dim]  Press any key to return...[/]")
    _getch()
    state.paused = was_paused

# ===========================================================
#  KEYBOARD INPUT
# ===========================================================
def _getch() -> str:
    try:
        import termios, tty
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return ""

def input_handler():
    while state.running or state.paused:
        ch = _getch().lower()
        if not ch:
            time.sleep(0.1)
            continue
        if ch in ("q", "\x03", "\x1c"):
            state.running  = False
            state.paused   = False
            state.status   = "Stopping -- saving session..."
            logger.warning("SESSION END (user quit)  bets=%d  profit=%+.8f  balance=%.8f",
                        state.total_bets, state.profit, state.current_balance)
            _db_save_session()
        elif ch == "p":
            state.paused = True
            state.status = "PAUSED"
        elif ch == "r":
            state.paused = False
            state.status = "Resumed"
        elif ch == "h":
            show_history()
        elif ch == "t":
            show_tweak()
        elif ch == "e":
            show_export()

def _print_tweak_help():
    """Compact reference of tweakable params + example values."""
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("Param",   width=20)
    t.add_column("Example", width=28)
    t.add_column("Notes",   width=32)
    rows = [
        ("delay <sec>",          "delay 0.1",          "bet_delay"),
        ("bbpct <0-1>",          "bbpct 0.001",        "basebet_pct (0 disables)"),
        ("sdloss / sdwin",       "sdloss 3:0.5",       "'N:sec' streak delay"),
        ("sdbets / sbloss",      "sdbets 100:0.5",     "streak-bet reduction"),
        ("maxprofit",            "maxprofit 0.01",     "'off' to disable"),
        ("maxloss / maxbets",    "maxbets 5000",       "instant stop condition"),
        ("maxwins / minbalance", "maxwins 1000",       "instant stop condition"),
        ("msbets / mswins",      "msbets 100",         "milestone thresholds"),
        ("mslosses / msprofit",  "msprofit 0.001",     "milestone thresholds"),
        ("pi / pt",              "pt 0.001",           "profit increment"),
        ("recurring on/off",     "recurring on",       "restart on non-loss stop"),
        ("recdelay",             "recdelay 60",        "pause between cycles (s)"),
        ("srloss / srdelay",     "srloss 20",          "restart on win after N losses"),
        ("purgedays / dbmode",   "purgedays 7",        "retention 1-30 / async|sync"),
        ("basebet <amount>",     "basebet 0.0001",     "applies to the next bet"),
        ("multiplier",           "multiplier 3.0",     "dice target re-derived"),
        ("lossmult / winmult",   "lossmult 2.01",      "strategy multipliers"),
    ]
    for p, ex, note in rows:
        t.add_row(f"[cyan]{p}[/]", f"[yellow]{ex}[/]", f"[dim]{note}[/]")
    console.print(t)

def show_export():
    """`E` hotkey: pause, ask for a path, export this session, restore pause state."""
    was_paused = state.paused
    state.paused = True
    time.sleep(0.15)
    console.clear()
    console.print(Rule("[bold cyan]Export session[/]"))
    default_path = os.path.expanduser(f"~/stake-session-{state.session_id}.csv")
    console.print(f"  [dim]Default[/] [cyan]{default_path}[/]")
    console.print("  [dim]Enter keeps the default, 'back' cancels, '.json' switches format.[/]\n")
    try:
        raw = input("  Path: ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = "back"
    if raw.lower() not in ("back", "cancel", "q", "quit"):
        path = raw or default_path
        try:
            n, fmt = _export_session(state.session_id, path)
            console.print(f"  [green]OK[/] Exported [bold]{n}[/] bets to [cyan]{escape(path)}[/] ({fmt.upper()})")
            logger.info("EXPORT: session=%d format=%s bets=%d path=%s",
                        state.session_id, fmt, n, path)
        except (OSError, ValueError, sqlite3.Error) as e:
            console.print(f"  [red]Export failed:[/] {escape(str(e))}")
        time.sleep(2)
    state.paused = was_paused


def show_tweak():
    """Modal runtime tweak (WolfBet v2.19.0): pause, apply `param value`, restore."""
    was_paused = state.paused
    state.paused = True
    time.sleep(0.15)

    console.clear()
    console.print(Rule("[bold cyan]Runtime tweak[/]"))
    _print_tweak_help()

    while True:
        console.print("\n[dim]Format: [/][cyan]param value[/]  [dim]|  'back' to cancel[/]")
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw or raw.lower() in ("back", "q", "quit"):
            break
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            console.print("  [red]Missing value.[/] Try: [cyan]delay 0.1[/]")
            continue
        param = _TWEAK_ALIAS.get(parts[0].lower(), parts[0].lower())
        if param not in _TWEAK_PARAMS:
            console.print(f"  [red]Unknown param:[/] [cyan]{param}[/]")
            continue
        try:
            msgs = _apply_mutation({param: parts[1]})
        except (ValueError, TypeError) as e:
            console.print(f"  [red]Invalid value:[/] {e}")
            continue
        for m in msgs:
            console.print(f"  [green]✓[/] {m}")
            logger.info("TWEAK: %s", m)
        time.sleep(2.0)
        break

    console.clear()
    state.paused = was_paused

# ===========================================================
#  SETUP WIZARD
# ===========================================================
_BACK = object()

def _ask(prompt_text: str, default: str = "", choices: list = None) -> str:
    hint = f" [bold yellow]{default}[/]" if default else ""
    console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
    while True:
        try:
            raw = input(": ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if raw.lower() == "back":
            return _BACK
        if not raw:
            if default:
                return default
            if choices:
                console.print(f"    [red]Choose from:[/] {', '.join(choices)}")
                console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
                continue
            return default
        if choices and raw.lower() not in [c.lower() for c in choices]:
            console.print(f"    [red]Choose from:[/] {', '.join(choices)}")
            console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
            continue
        return raw

def _ask_optional(prompt_text: str, current) -> str:
    if current is not None:
        hint = f" [bold yellow]{current}[/]  [dim](type 'none' to clear)[/]"
    else:
        hint = " [dim]none[/]"
    console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
    try:
        raw = input(": ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if raw.lower() == "back":
        return _BACK
    if raw.lower() in ("none", "clear", "off", "disable", "0"):
        return ""
    if not raw:
        return str(current) if current is not None else ""
    return raw


def _build_one_rule():
    console.print("  [bold cyan]-- New Rule --[/]")
    console.print("    [dim]Condition type:[/]")
    for k, name in COND_TYPES.items():
        label = {"sequence": "Sequence (streaks/counts)", "profit": "Profit / Loss / Balance",
                 "bet": "Bet properties"}.get(name, name)
        console.print(f"      [bold yellow][{k}][/] {label}")

    ct = _ask("  Condition type", choices=list(COND_TYPES.keys()))
    if ct is _BACK:
        return _BACK
    cond_type = COND_TYPES[ct]

    cond_field = ""
    cond_mode = ""
    cond_value = 0.0
    cond_trigger = ""

    if cond_type == "sequence":
        console.print("    [dim]When should this fire?[/]")
        for k, (_, label) in SEQ_MODES.items():
            console.print(f"      [bold yellow][{k}][/] {label}")
        sm = _ask("  Sequence mode", choices=list(SEQ_MODES.keys()))
        if sm is _BACK:
            return _BACK
        cond_mode = SEQ_MODES[sm][0]

        nv = _ask("  N value (number)", default="1")
        if nv is _BACK:
            return _BACK
        try:
            cond_value = float(nv)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

        console.print("    [dim]On which event?[/]")
        for k, t in SEQ_TRIGGERS.items():
            console.print(f"      [bold yellow][{k}][/] {t}")
        st = _ask("  Event", choices=list(SEQ_TRIGGERS.keys()))
        if st is _BACK:
            return _BACK
        cond_trigger = SEQ_TRIGGERS[st]

    elif cond_type == "profit":
        console.print("    [dim]Check what?[/]")
        for k, name in PROFIT_FIELDS.items():
            console.print(f"      [bold yellow][{k}][/] {name}")
        pf = _ask("  Field", choices=list(PROFIT_FIELDS.keys()))
        if pf is _BACK:
            return _BACK
        cond_field = PROFIT_FIELDS[pf]

        console.print("    [dim]Comparison:[/]")
        for k, (_, sym) in CMP_OPS.items():
            console.print(f"      [bold yellow][{k}][/] {sym}")
        op = _ask("  Operator", choices=list(CMP_OPS.keys()))
        if op is _BACK:
            return _BACK
        cond_mode = CMP_OPS[op][0]

        vl = _ask(f"  Value ({state.currency.upper()})", default="0")
        if vl is _BACK:
            return _BACK
        try:
            cond_value = float(vl)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    elif cond_type == "bet":
        console.print("    [dim]Check what?[/]")
        for k, name in BET_FIELDS.items():
            console.print(f"      [bold yellow][{k}][/] {name}")
        bf = _ask("  Field", choices=list(BET_FIELDS.keys()))
        if bf is _BACK:
            return _BACK
        cond_field = BET_FIELDS[bf]

        console.print("    [dim]Comparison:[/]")
        for k, (_, sym) in CMP_OPS.items():
            console.print(f"      [bold yellow][{k}][/] {sym}")
        op = _ask("  Operator", choices=list(CMP_OPS.keys()))
        if op is _BACK:
            return _BACK
        cond_mode = CMP_OPS[op][0]

        unit = "%" if cond_field == "winchance" else ("x" if cond_field == "payout" else "")
        vl = _ask(f"  Value{(' (' + unit + ')') if unit else ''}", default="0")
        if vl is _BACK:
            return _BACK
        try:
            cond_value = float(vl)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    console.print("    [dim]Then do what?[/]")
    for k, (_, label) in RULE_ACTIONS.items():
        console.print(f"      [bold yellow][{k:>2}][/] {label}")
    ak = _ask("  Action", choices=list(RULE_ACTIONS.keys()))
    if ak is _BACK:
        return _BACK
    action = RULE_ACTIONS[ak][0]

    action_value = 0.0
    needs_value = action in ("increase_amount", "decrease_amount", "add_amount",
                             "deduct_amount", "set_amount", "set_winchance",
                             "increase_wc", "decrease_wc")
    if needs_value:
        if action in ("increase_amount", "decrease_amount", "increase_wc", "decrease_wc"):
            unit_hint = "%"
        elif action == "set_winchance":
            unit_hint = "% win chance"
        else:
            unit_hint = state.currency.upper()
        av = _ask(f"  Value ({unit_hint})", default="0")
        if av is _BACK:
            return _BACK
        try:
            action_value = float(av)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    rule = StrategyRule(
        cond_type=cond_type, cond_field=cond_field, cond_mode=cond_mode,
        cond_value=cond_value, cond_trigger=cond_trigger,
        action=action, action_value=action_value,
    )
    rule.description = _describe_rule(rule)
    return rule


def _edit_one_rule(old: StrategyRule):
    """Edit an existing rule part-by-part. Returns updated StrategyRule, _BACK, or None."""
    r = StrategyRule.from_dict(old.to_dict())
    act_label = next((v[1] for v in RULE_ACTIONS.values() if v[0] == r.action), r.action)

    while True:
        console.print()
        console.print(f"    [bold]{r.description}[/]")
        console.print()

        parts = []
        if r.cond_type == "sequence":
            mode_label = dict(every="Every N", every_streak="Every streak of N",
                              first_streak="First streak of N",
                              streak_above="Streak above N", streak_below="Streak below N").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Mode",       mode_label))
            parts.append(("N value",    str(int(r.cond_value))))
            parts.append(("Event",      r.cond_trigger))
        elif r.cond_type == "profit":
            op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Field",      r.cond_field))
            parts.append(("Operator",   op_sym))
            parts.append(("Value",      str(r.cond_value)))
        elif r.cond_type == "bet":
            op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Field",      r.cond_field))
            parts.append(("Operator",   op_sym))
            parts.append(("Value",      str(r.cond_value)))

        act_label = next((v[1] for v in RULE_ACTIONS.values() if v[0] == r.action), r.action)
        parts.append(("Action",     act_label))
        if r.action_value:
            parts.append(("Action value", str(r.action_value)))

        for i, (label, val) in enumerate(parts, 1):
            console.print(f"      [bold yellow][{i}][/] {label:14s} [cyan]{val}[/]")
        save_n = len(parts) + 1
        replace_n = save_n + 1
        console.print(f"      [bold yellow][{save_n}][/] [green]Save changes[/]")
        console.print(f"      [bold yellow][{replace_n}][/] [yellow]Rebuild from scratch[/]")

        valid = [str(i) for i in range(1, replace_n + 1)]
        pick = _ask("    Edit which part?", default=str(save_n), choices=valid)
        if pick is _BACK:
            return _BACK
        pick = int(pick)

        if pick == save_n:
            r.description = _describe_rule(r)
            return r

        if pick == replace_n:
            console.print("    [dim]Building replacement rule...[/]")
            replacement = _build_one_rule()
            if replacement is _BACK:
                continue
            return replacement

        label = parts[pick - 1][0]

        if label == "Condition":
            console.print("      [dim]Change condition type:[/]")
            for k, name in COND_TYPES.items():
                tag = {"sequence": "Sequence", "profit": "Profit / Loss / Balance",
                       "bet": "Bet properties"}.get(name, name)
                console.print(f"        [bold yellow][{k}][/] {tag}")
            cur_key = next((k for k, v in COND_TYPES.items() if v == r.cond_type), "1")
            ct = _ask("      Type", default=cur_key, choices=list(COND_TYPES.keys()))
            if ct is _BACK:
                continue
            new_type = COND_TYPES[ct]
            if new_type != r.cond_type:
                r.cond_type = new_type
                if new_type == "sequence":
                    r.cond_field = ""
                    r.cond_mode = "every"
                    r.cond_value = 1.0
                    r.cond_trigger = "win"
                elif new_type == "profit":
                    r.cond_field = "profit"
                    r.cond_mode = "gte"
                    r.cond_value = 0.0
                    r.cond_trigger = ""
                elif new_type == "bet":
                    r.cond_field = "amount"
                    r.cond_mode = "gte"
                    r.cond_value = 0.0
                    r.cond_trigger = ""

        elif label == "Mode":
            console.print("      [dim]Sequence mode:[/]")
            for k, (_, ml) in SEQ_MODES.items():
                console.print(f"        [bold yellow][{k}][/] {ml}")
            cur_key = next((k for k, v in SEQ_MODES.items() if v[0] == r.cond_mode), "1")
            sm = _ask("      Mode", default=cur_key, choices=list(SEQ_MODES.keys()))
            if sm is _BACK:
                continue
            r.cond_mode = SEQ_MODES[sm][0]

        elif label == "N value":
            nv = _ask("      N value", default=str(int(r.cond_value)))
            if nv is _BACK:
                continue
            try:
                r.cond_value = float(nv)
            except ValueError:
                console.print("      [red]Invalid number[/]")

        elif label == "Event":
            console.print("        [bold yellow][1][/] win  [bold yellow][2][/] loss  [bold yellow][3][/] bet")
            cur_key = {"win": "1", "loss": "2", "bet": "3"}.get(r.cond_trigger, "1")
            st = _ask("      Event", default=cur_key, choices=["1", "2", "3"])
            if st is _BACK:
                continue
            r.cond_trigger = SEQ_TRIGGERS[st]

        elif label == "Field":
            if r.cond_type == "profit":
                console.print("        [bold yellow][1][/] profit  [bold yellow][2][/] loss  [bold yellow][3][/] balance")
                cur_key = next((k for k, v in PROFIT_FIELDS.items() if v == r.cond_field), "1")
                pf = _ask("      Field", default=cur_key, choices=list(PROFIT_FIELDS.keys()))
                if pf is _BACK:
                    continue
                r.cond_field = PROFIT_FIELDS[pf]
            elif r.cond_type == "bet":
                for k, name in BET_FIELDS.items():
                    console.print(f"        [bold yellow][{k}][/] {name}")
                cur_key = next((k for k, v in BET_FIELDS.items() if v == r.cond_field), "1")
                bf = _ask("      Field", default=cur_key, choices=list(BET_FIELDS.keys()))
                if bf is _BACK:
                    continue
                r.cond_field = BET_FIELDS[bf]

        elif label == "Operator":
            for k, (_, sym) in CMP_OPS.items():
                console.print(f"        [bold yellow][{k}][/] {sym}")
            cur_key = next((k for k, v in CMP_OPS.items() if v[0] == r.cond_mode), "1")
            op = _ask("      Operator", default=cur_key, choices=list(CMP_OPS.keys()))
            if op is _BACK:
                continue
            r.cond_mode = CMP_OPS[op][0]

        elif label == "Value":
            vl = _ask("      Value", default=str(r.cond_value))
            if vl is _BACK:
                continue
            try:
                r.cond_value = float(vl)
            except ValueError:
                console.print("      [red]Invalid number[/]")

        elif label == "Action":
            console.print("      [dim]Change action:[/]")
            for k, (_, al) in RULE_ACTIONS.items():
                console.print(f"        [bold yellow][{k:>2}][/] {al}")
            cur_key = next((k for k, v in RULE_ACTIONS.items() if v[0] == r.action), "1")
            ak = _ask("      Action", default=cur_key, choices=list(RULE_ACTIONS.keys()))
            if ak is _BACK:
                continue
            new_action = RULE_ACTIONS[ak][0]
            if new_action != r.action:
                r.action = new_action
                r.action_value = 0.0

        elif label == "Action value":
            av = _ask("      Action value", default=str(r.action_value))
            if av is _BACK:
                continue
            try:
                r.action_value = float(av)
            except ValueError:
                console.print("      [red]Invalid number[/]")


def setup_wizard():
    console.clear()
    console.print()
    console.print(Align.center(
        Panel(
            f"[bold cyan]Stake AutoBot  v{VERSION}[/]\n"
            "[dim]Multi-game auto-bettor with live TUI dashboard[/]",
            box=box.DOUBLE, expand=False, padding=(1, 6), style="magenta"
        )
    ))
    console.print()
    console.print("[dim]  Tips: arrow keys work  |  type 'back' for previous step  |  'none' to clear a value[/]")
    console.print()

    has_saved = load_config()
    saved_token = state.access_token
    saved_lockdown = state.lockdown_token
    saved_cookie = state.cookie

    if has_saved and saved_token:
        console.print("[green]  Found saved config from last session:[/]")
        game_label = GAMES.get(state.game, {}).get("label", state.game)
        console.print(f"    [dim]Game:[/]       [yellow]{game_label}[/]")
        console.print(f"    [dim]Currency:[/]    [yellow]{state.currency.upper()}[/]")
        console.print(f"    [dim]Strategy:[/]    [yellow]{state.strategy}[/]")
        console.print(f"    [dim]Multiplier:[/]  [yellow]{state.multiplier_target}x[/]")
        console.print(f"    [dim]Base bet:[/]    [yellow]{state.base_bet:.8f}[/]")
        console.print(f"    [dim]Token:[/]       [yellow]{saved_token[:8]}...{saved_token[-4:]}[/]")
        has_cookie = "yes" if saved_cookie else "no"
        console.print(f"    [dim]Cookie:[/]      [yellow]{has_cookie}[/]")
        console.print()

    def step_auth():
        nonlocal saved_token, saved_lockdown, saved_cookie
        console.print(Rule("[bold cyan]Step 1 / 8 -- Authentication[/]"))
        console.print("  [dim]You need 3 things from browser DevTools (Network tab):[/]")
        console.print("    [yellow]1.[/] x-access-token  [dim](request header)[/]")
        console.print("    [yellow]2.[/] x-lockdown-token  [dim](request header)[/]")
        console.print("    [yellow]3.[/] Cookie  [dim](full cookie string -- needed for Cloudflare)[/]")
        console.print()
        console.print("  [dim]Tip: In DevTools > Network, click any request to stake.com,[/]")
        console.print("  [dim]go to Headers tab, and copy the values from Request Headers.[/]")
        console.print()
        console.print("  [dim yellow]⚠  CF cookies are bound to your browser's IP. If this bot runs[/]")
        console.print("  [dim yellow]   on a server, connection will fail unless you set a proxy that[/]")
        console.print("  [dim yellow]   routes through your local machine's IP.[/]")
        console.print()

        if saved_token:
            console.print(f"  Saved token: [yellow]{saved_token[:8]}...{saved_token[-4:]}[/]")
            v = _ask("Use saved credentials? (y/n)", default="y", choices=["y", "n"])
            if v is _BACK:
                return False
            if v.lower() == "y":
                state.access_token = saved_token
                state.lockdown_token = saved_lockdown
                state.cookie = saved_cookie
            else:
                v = _ask("Paste your x-access-token")
                if v is _BACK:
                    return False
                state.access_token = v
                saved_token = v

                v = _ask("Paste your x-lockdown-token")
                if v is _BACK:
                    return False
                state.lockdown_token = v
                saved_lockdown = v

                console.print()
                console.print("  [dim]Now paste the full cookie string (the entire 'cookie:' header value).[/]")
                console.print("  [dim]It's long -- that's normal. Just paste the whole thing.[/]")
                v = _ask("Paste your cookie string")
                if v is _BACK:
                    return False
                state.cookie = v
                saved_cookie = v
        else:
            v = _ask("Paste your x-access-token")
            if v is _BACK:
                return False
            state.access_token = v
            saved_token = v

            v = _ask("Paste your x-lockdown-token")
            if v is _BACK:
                return False
            state.lockdown_token = v
            saved_lockdown = v

            console.print()
            console.print("  [dim]Now paste the full cookie string (the entire 'cookie:' header value).[/]")
            console.print("  [dim]It's long -- that's normal. Just paste the whole thing.[/]")
            v = _ask("Paste your cookie string")
            if v is _BACK:
                return False
            state.cookie = v
            saved_cookie = v

        # Proxy (optional -- needed when server IP is CF-blocked)
        saved_proxy = state.proxy or ""
        console.print()
        console.print("  [dim]Proxy (optional -- socks5://host:port or http://user:pass@host:port).[/]")
        console.print("  [dim]Required if this bot runs on a server with a blocked IP. Press Enter to skip.[/]")
        while True:
            v = _ask("Proxy URL", default=saved_proxy or "")
            if v is _BACK:
                break
            try:
                state.proxy = validate_proxy(v.strip()) or ""
            except ValueError as e:
                console.print(f"    [red]{e}[/]")
                continue
            _recreate_http()
            break

        return True

    def step_game():
        console.print(Rule("[bold cyan]Step 2 / 8 -- Game Selection[/]"))
        game_keys = list(GAMES.keys())
        for i, gk in enumerate(game_keys, 1):
            g = GAMES[gk]
            console.print(f"    [bold yellow][{i}][/] [cyan]{g['label']}[/]")
        console.print()

        choices = [str(i) for i in range(1, len(game_keys) + 1)]
        cur_idx = game_keys.index(state.game) + 1 if state.game in game_keys else 1
        v = _ask("Select game", default=str(cur_idx), choices=choices)
        if v is _BACK:
            return False
        state.game = game_keys[int(v) - 1]

        # Test connection with this game
        console.print(f"  [dim]Testing connection ({GAMES[state.game]['label']})...[/]")
        try:
            api_test_connection()
            console.print("[green]  Connected![/]\n")
        except Exception as e:
            msg = str(e)
            console.print(f"[red]  Connection failed: {msg}[/]")
            if "Cloudflare" in msg:
                console.print("[yellow]  Tip: in browser DevTools, copy the full Cookie header value and paste it at the cookie prompt.[/]\n")
            else:
                console.print("[yellow]  Check your tokens and try again.[/]\n")
            state.access_token = ""
            saved_token = ""
            return False
        return True

    def step_currency():
        console.print(Rule("[bold cyan]Step 3 / 8 -- Currency & Balance[/]"))

        # Fetch balances from API
        console.print("  [dim]Fetching balances from Stake.com...[/]")
        balances = api_get_balances()

        if balances:
            console.print("  [green]Balances found:[/]")
            bal_map = {}
            for b in balances:
                cur = b["currency"].lower()
                amt = b["amount"]
                bal_map[cur] = amt
                console.print(f"    [yellow]{cur.upper():>6}[/]  {amt:.8f}")
            console.print()

            v = _ask("Select currency", default=state.currency, choices=CURRENCIES)
            if v is _BACK:
                return False
            state.currency = v.lower()

            if state.currency in bal_map:
                bal = bal_map[state.currency]
                console.print(f"  [green]Using API balance: {bal:.8f} {state.currency.upper()}[/]")
                v = _ask("Accept this balance? (or enter a different one)",
                         default=str(bal))
            else:
                console.print(f"  [yellow]No {state.currency.upper()} balance found on API.[/]")
                v = _ask(f"Enter your current {state.currency.upper()} balance",
                         default=str(state.start_balance) if state.start_balance else "")
        else:
            console.print("  [yellow]Could not fetch balances from API.[/]")
            console.print("  [dim]Enter your balance manually (check on stake.com).[/]")
            console.print()

            v = _ask("Select currency", default=state.currency, choices=CURRENCIES)
            if v is _BACK:
                return False
            state.currency = v.lower()

            v = _ask(f"Enter your current {state.currency.upper()} balance",
                     default=str(state.start_balance) if state.start_balance else "")

        if v is _BACK:
            return False
        try:
            bal = float(v)
        except ValueError:
            console.print("    [red]Enter a valid number[/]")
            return False
        state.start_balance   = bal
        state.current_balance = bal
        state.highest_balance = bal
        state.lowest_balance  = bal
        console.print(f"  [green]Balance: {bal:.8f} {state.currency.upper()}[/]\n")
        return True

    def step_bet_config():
        console.print(Rule("[bold cyan]Step 4 / 8 -- Bet Configuration[/]"))
        game_label = GAMES[state.game]["label"]

        if state.game == "limbo":
            console.print(f"  [dim]Game: {game_label} -- pick a multiplier target, win if result >= target[/]")
            console.print()
            console.print("  [dim]Multiplier examples (Limbo, ~99% RTP):[/]")
            for mult, chance in [(1.5, 66.0), (2.0, 49.5), (3.0, 33.0), (5.0, 19.8), (10.0, 9.9), (100.0, 0.99)]:
                console.print(f"    [yellow]{mult:6.1f}x[/]  ->  ~{chance:.1f}% win")
            console.print()

            while True:
                v = _ask("Multiplier target", default=str(state.multiplier_target))
                if v is _BACK:
                    return False
                try:
                    m = float(v)
                except ValueError:
                    console.print("    [red]Enter a valid number[/]")
                    continue
                if m < 1.01:
                    console.print("    [red]Multiplier must be at least 1.01[/]")
                    continue
                state.multiplier_target = m
                break
            wc = 99.0 / state.multiplier_target
            console.print(f"    [dim]-> Win chance:[/] [yellow]{wc:.2f}%[/]\n")

        elif state.game == "dice":
            console.print(f"  [dim]Game: {game_label} -- roll above/below a target number (0-100)[/]")
            console.print()

            v = _ask("Bet direction", default=state.dice_condition, choices=["above", "below"])
            if v is _BACK:
                return False
            state.dice_condition = v.lower()

            console.print()
            console.print("  [dim]Multiplier examples (Dice, 99% RTP):[/]")
            for mult, target_above in [(2.0, 50.5), (3.0, 67.0), (5.0, 80.2), (10.0, 90.1), (1.5, 34.0)]:
                wc = 99.0 / mult
                if state.dice_condition == "above":
                    tgt = round(100.0 - wc, 2)
                else:
                    tgt = round(wc, 2)
                console.print(f"    [yellow]{mult:5.2f}x[/]  target={tgt:6.2f}  ~{wc:.1f}% win")
            console.print()

            while True:
                v = _ask("Multiplier", default=str(state.multiplier_target))
                if v is _BACK:
                    return False
                try:
                    m = float(v)
                except ValueError:
                    console.print("    [red]Enter a valid number[/]")
                    continue
                if m < 1.01:
                    console.print("    [red]Multiplier must be at least 1.01[/]")
                    continue
                state.multiplier_target = m
                break

            # Calculate dice target from multiplier
            wc = 99.0 / state.multiplier_target
            if state.dice_condition == "above":
                state.dice_target = round(100.0 - wc, 2)
            else:
                state.dice_target = round(wc, 2)
            console.print(f"    [dim]-> Target:[/] [yellow]{state.dice_condition} {state.dice_target}[/]"
                          f"  [dim]Win chance:[/] [yellow]{wc:.2f}%[/]\n")

        console.print(f"  [dim]Minimum bet: {MIN_BET} | Use 0 for test bets (no real money)[/]")
        v = _ask(f"Base bet amount ({state.currency.upper()})", default=str(state.base_bet))
        if v is _BACK:
            return False
        bet = float(v)
        if bet > 0 and bet < MIN_BET:
            console.print(f"    [yellow]Rounding up to minimum: {MIN_BET}[/]")
            bet = MIN_BET
        state.base_bet    = bet
        state.current_bet = state.base_bet
        return True

    def step_strategy():
        console.print(Rule("[bold cyan]Step 5 / 8 -- Betting Strategy[/]"))
        for k, (name, desc) in STRATEGIES.items():
            console.print(f"    [bold yellow][{k}][/] [cyan]{name:20s}[/]  [dim]{desc}[/]")
        console.print()

        v = _ask("Select strategy", default=state.strategy_key, choices=list(STRATEGIES.keys()))
        if v is _BACK:
            return False
        state.strategy_key = v
        state.strategy     = STRATEGIES[v][0]

        if v in ("2", "3", "5", "6"):
            console.print()
            if v in ("2", "6"):
                console.print(f"  [dim]On loss: multiply bet by[/] [yellow]{state.loss_mult}x[/]")
                lm = _ask("  Loss multiplier", default=str(state.loss_mult))
                if lm is _BACK:
                    return False
                try:
                    state.loss_mult = float(lm)
                except ValueError:
                    pass
            if v in ("3", "5"):
                console.print(f"  [dim]On win: multiply bet by[/] [yellow]{state.win_mult}x[/]")
                wm = _ask("  Win multiplier", default=str(state.win_mult))
                if wm is _BACK:
                    return False
                try:
                    state.win_mult = float(wm)
                except ValueError:
                    pass
            console.print()

        if v == "6":
            console.print()
            console.print("  [bold cyan]Delay Martingale[/] -- flat bet through the first N")
            console.print("  consecutive losses, then start doubling.")
            console.print()
            d = _ask("  Losses before doubling starts", default=str(state.delay_martin_threshold))
            if d is _BACK:
                return False
            state.delay_martin_threshold = int(d)
            console.print(f"    [green]-> Flat for {state.delay_martin_threshold} losses, then Martingale[/]\n")

        elif v == "7":
            console.print()
            console.print("  [bold cyan]Rule-Based Strategy[/] -- build custom conditions & actions")
            console.print("  [dim]Each rule: IF <condition> THEN <action>[/]")
            console.print()

            while True:
                if state.custom_rules:
                    console.print(f"  [yellow]Current rules ({len(state.custom_rules)}):[/]")
                    for i, r in enumerate(state.custom_rules, 1):
                        console.print(f"    [dim]{i}.[/] {r.description}")
                    console.print()
                    console.print("    [bold yellow][1][/] Add rule   [bold yellow][2][/] Edit rule   [bold yellow][3][/] Delete rule   [bold yellow][4][/] Clear all   [bold yellow][5][/] Continue")
                    v2 = _ask("  Select", default="5", choices=["1", "2", "3", "4", "5"])
                    if v2 is _BACK:
                        return False
                    if v2 == "5":
                        break
                    elif v2 == "1":
                        rule = _build_one_rule()
                        if rule is _BACK:
                            continue
                        if rule is not None:
                            state.custom_rules.append(rule)
                            console.print(f"    [green]+ Rule {len(state.custom_rules)}:[/] {rule.description}\n")
                    elif v2 == "2":
                        if len(state.custom_rules) == 1:
                            idx = 0
                        else:
                            en = _ask(f"  Rule # to edit (1-{len(state.custom_rules)})")
                            if en is _BACK:
                                continue
                            try:
                                idx = int(en) - 1
                                if not (0 <= idx < len(state.custom_rules)):
                                    console.print("    [red]Invalid rule number[/]")
                                    continue
                            except ValueError:
                                console.print("    [red]Enter a number[/]")
                                continue
                        old = state.custom_rules[idx]
                        console.print(f"    [dim]Editing:[/] {old.description}")
                        replacement = _edit_one_rule(old)
                        if replacement is _BACK:
                            continue
                        if replacement is not None:
                            state.custom_rules[idx] = replacement
                            console.print(f"    [green]Updated:[/] {replacement.description}\n")
                    elif v2 == "3":
                        if len(state.custom_rules) == 1:
                            idx = 0
                        else:
                            en = _ask(f"  Rule # to delete (1-{len(state.custom_rules)})")
                            if en is _BACK:
                                continue
                            try:
                                idx = int(en) - 1
                                if not (0 <= idx < len(state.custom_rules)):
                                    console.print("    [red]Invalid rule number[/]")
                                    continue
                            except ValueError:
                                console.print("    [red]Enter a number[/]")
                                continue
                        removed = state.custom_rules.pop(idx)
                        console.print(f"    [red]Deleted:[/] {removed.description}\n")
                    elif v2 == "4":
                        state.custom_rules = []
                        console.print("    [yellow]All rules cleared.[/]\n")
                else:
                    console.print("  [dim]No rules yet. Let's build some.[/]\n")
                    rule = _build_one_rule()
                    if rule is _BACK:
                        return False
                    if rule is not None:
                        state.custom_rules.append(rule)
                        console.print(f"    [green]+ Rule {len(state.custom_rules)}:[/] {rule.description}\n")
                    else:
                        break

            if state.custom_rules:
                state.custom_rules_text = json.dumps([r.to_dict() for r in state.custom_rules])
                console.print(f"  [bold green]-> {len(state.custom_rules)} rule(s) configured[/]\n")
            else:
                console.print("  [yellow]No rules configured -- will use Flat bet.[/]\n")

        console.print()
        return True

    def step_stop_conditions():
        console.print(Rule("[bold cyan]Step 6 / 8 -- Stop Conditions[/]"))
        console.print("  [dim]Enter to keep current  |  'none' to clear  |  'back' for previous step[/]\n")

        v = _ask_optional(f"Stop when profit reaches ({state.currency.upper()})", state.max_profit)
        if v is _BACK:
            return False
        state.max_profit = float(v) if v else None

        v = _ask_optional(f"Stop when loss exceeds ({state.currency.upper()})", state.max_loss)
        if v is _BACK:
            return False
        state.max_loss = float(v) if v else None

        v = _ask_optional("Stop after N bets", state.max_bets)
        if v is _BACK:
            return False
        state.max_bets = int(v) if v else None

        v = _ask_optional("Stop after N wins", state.max_wins)
        if v is _BACK:
            return False
        state.max_wins = int(v) if v else None

        v = _ask_optional(f"Stop if balance drops below ({state.currency.upper()})", state.stop_on_balance)
        if v is _BACK:
            return False
        state.stop_on_balance = float(v) if v else None

        console.print("\n  [bold]Profit-based bet increment[/] [dim]-- auto-raise base bet as profit grows[/]")
        v = _ask_optional(f"Profit threshold to trigger increment ({state.currency.upper()})", state.profit_threshold)
        if v is _BACK:
            return False
        state.profit_threshold = float(v) if v else None

        v = _ask_optional(f"Increment amount to add to base bet ({state.currency.upper()})", state.profit_increment)
        if v is _BACK:
            return False
        state.profit_increment = float(v) if v else None

        if state.profit_threshold and state.profit_increment:
            v = _ask("Measure profit per session or across the whole run? (session/run)",
                     default=state.profit_increment_scope or "session",
                     choices=list(PROFIT_INCREMENT_SCOPES) + ["back"])
            if v is _BACK or v == "back":
                return False
            state.profit_increment_scope = parse_scope(v)

        if state.profit_threshold:
            state.next_profit_milestone = next_milestone(0, state.profit_threshold)

        console.print()
        return True

    def step_confirm():
        console.print(Rule("[bold green]Step 8 / 8 -- Review & Confirm[/]"))
        game_label = GAMES[state.game]["label"]
        wc = _get_win_chance()

        rows = [
            ("Platform",         f"Stake.com -- {game_label}"),
            ("Currency",         f"{state.currency.upper()}"),
            ("Balance",          f"{state.start_balance:.8f}"),
        ]
        if state.game == "limbo":
            rows.append(("Multiplier", f"{state.multiplier_target}x  (win chance {wc:.2f}%)"))
        elif state.game == "dice":
            rows.append(("Dice target", f"{state.dice_condition} {state.dice_target}"))
            rows.append(("Multiplier",  f"{state.multiplier_target}x  (win chance {wc:.2f}%)"))
        rows += [
            ("Base bet",         f"{state.base_bet:.8f} {state.currency.upper()}" +
                                 (" [TEST MODE]" if state.base_bet == 0 else "")),
            ("Strategy",         state.strategy),
        ]
        if state.strategy_key == "6":
            rows.append(("Delay",      f"flat for {state.delay_martin_threshold} losses"))
        if state.strategy_key == "7" and state.custom_rules:
            rows.append(("Rules",      f"{len(state.custom_rules)} custom rule(s)"))
        rows += [
            ("Max profit",       str(state.max_profit) if state.max_profit else "--"),
            ("Max loss",         str(state.max_loss)   if state.max_loss   else "--"),
            ("Max bets",         str(state.max_bets)   if state.max_bets   else "Unlimited"),
            ("Max wins",         str(state.max_wins)   if state.max_wins   else "Unlimited"),
            ("Min balance",      str(state.stop_on_balance) if state.stop_on_balance else "--"),
            ("Profit increment", f"+{state.profit_increment:.8f} every {state.profit_threshold} "
                                 f"{'run ' if state.profit_increment_scope == 'run' else ''}profit"
                if state.profit_increment and state.profit_threshold else "--"),
        ]
        for k, val in rows:
            console.print(f"  [dim]{k:18s}[/] [yellow]{val}[/]")

        if state.strategy_key == "7" and state.custom_rules:
            console.print()
            for i, r in enumerate(state.custom_rules, 1):
                console.print(f"    [dim]{i}.[/] [green]{r.description}[/]")

        # v1.12.0 (WolfBet v2.22.1): heads-up for risky combinations, never blocking
        risk_warnings = _check_risky_config()
        if risk_warnings:
            console.print()
            body_lines = [Text.from_markup(f"[yellow]•[/] {w}") for w in risk_warnings]
            console.print(Panel(
                Group(*body_lines),
                title="[bold yellow]Heads-up -- review before starting[/]",
                border_style="yellow",
            ))
            console.print("[dim]  These are safety hints, not blockers. Press y to proceed anyway.[/]")

        console.print()
        if state.base_bet == 0:
            console.print("[bold cyan]  TEST MODE: bets cost nothing, no real money wagered.[/]")
        else:
            console.print("[bold red]  WARNING: Real money will be wagered. Bet responsibly.[/]")
        console.print()

        v = _ask("Start auto-betting? (y/n/back)", default="n", choices=["y", "n", "back"])
        if v is _BACK or v.lower() == "back":
            return False
        if v.lower() != "y":
            console.print("[yellow]Aborted.[/]")
            sys.exit(0)

        save_config()
        console.print(f"[dim]  Config saved to {CONFIG_PATH}[/]\n")
        return True

    def step_advanced_tuning():
        """Optional session tuning (WolfBet v2.18.0 Step 5b): cycles, streaks, milestones."""
        console.print(Rule("[bold cyan]Step 6b / 8 -- Advanced session tuning (optional)[/]"))
        gate = _ask("Configure advanced tuning? (y/N)",
                    default="y" if (state.recurring or state.streak_reset_losses or state.basebet_pct
                                    or state.streak_delay_loss[0] or state.streak_delay_win[0]
                                    or state.streak_delay_bets[0] or state.streakbet_loss[0]) else "n",
                    choices=["y", "n"])
        if gate is _BACK:
            return False
        if gate.lower() != "y":
            console.print()
            return True

        console.print("  [dim]Enter to keep current  |  'none' to clear  |  'back' for previous step[/]\n")

        def _set(changes) -> bool:
            try:
                for m in _apply_mutation(changes):
                    if m.startswith("⚠"):
                        console.print(f"    [yellow]{m}[/]")
                return True
            except ValueError as e:
                console.print(f"    [red]{e}[/] [dim](kept the current value)[/]")
                return False

        rec = _ask("Recurring restart on non-loss stop? (y/N)",
                   default="y" if state.recurring else "n", choices=["y", "n"])
        if rec is _BACK:
            return False
        state.recurring = (rec.lower() == "y")

        prompts = [
            ("recurring_delay", "Pause between recurring cycles in seconds (0 = back-to-back)",
             state.recurring_delay or None),
            ("streak_reset_losses", "Restart session on first win after N losses in a row (blank/none = off)",
             state.streak_reset_losses),
        ]
        for attr, prompt, cur in prompts:
            v = _ask_optional(prompt, cur)
            if v is _BACK:
                return False
            _set({attr: v})
        if state.streak_reset_losses:
            v = _ask_optional("Pause before streak-reset restart in seconds",
                              state.streak_reset_delay or None)
            if v is _BACK:
                return False
            _set({"streak_reset_delay": v})

        v = _ask_optional("Base bet as % of balance (0-1, blank/none to disable)", state.basebet_pct or None)
        if v is _BACK:
            return False
        _set({"basebet_pct": v})

        for attr, prompt in [
            ("streak_delay_loss", "Streak delay on losses (format 'N:sec')"),
            ("streak_delay_win",  "Streak delay on wins (format 'N:sec')"),
            ("streak_delay_bets", "Streak delay every N bets (format 'N:sec')"),
            ("streakbet_loss",    "Streakbet loss reduction (format 'N:multiplier')"),
        ]:
            v = _ask_optional(prompt, _config_value(attr))
            if v is _BACK:
                return False
            _set({attr: v})

        for attr, prompt in [
            ("milestone_bets",   "Milestone every N bets"),
            ("milestone_wins",   "Milestone every N wins"),
            ("milestone_losses", "Milestone every N losses"),
        ]:
            v = _ask_optional(prompt, getattr(state, attr) or None)
            if v is _BACK:
                return False
            _set({attr: v})
        v = _ask_optional(f"Milestone every {state.currency.upper()} profit crossed",
                          state.milestone_profit or None)
        if v is _BACK:
            return False
        _set({"milestone_profit": v})

        v = _ask_optional("Keep bet rows for N days (1-30)", state.purge_days)
        if v is _BACK:
            return False
        if v:
            _set({"purge_days": v})
        v = _ask("Bet DB writer (async = batched, sync = per-bet)", default=state.db_write_mode,
                 choices=["async", "sync"])
        if v is _BACK:
            return False
        _set({"db_write_mode": v})

        console.print()
        return True

    def step_preset():
        console.print(Rule("[bold cyan]Step 7 / 8 -- Presets[/]"))
        presets = list_presets()
        if presets:
            console.print("  [dim]Saved presets:[/]")
            for name, data in presets.items():
                console.print(f"    [yellow]{name}[/]  [dim]-> {data.get('strategy', '?')} on "
                              f"{(data.get('currency') or '?').upper()}[/]")
            console.print()
        v = _ask("Save current config as preset? (name or 'skip')", default="skip")
        if v is _BACK:
            return False
        if v.lower() != "skip":
            save_preset(v)
            console.print(f"  [green]Saved preset '{v}'[/]\n")
        else:
            console.print()
        return True

    steps = [step_auth, step_game, step_currency, step_bet_config, step_strategy,
             step_stop_conditions, step_advanced_tuning, step_preset, step_confirm]
    i = 0
    while i < len(steps):
        ok = steps[i]()
        if ok:
            i += 1
        else:
            i = max(0, i - 1)

# ===========================================================
#  PRINT SESSION SUMMARY
# ===========================================================
def _print_summary():
    console.print()
    console.print(Rule("[bold cyan]Session Complete[/]"))
    console.print()
    if state._bets_this_sec > 0:
        if state._bets_this_sec > state.peak_bps:
            state.peak_bps = state._bets_this_sec
        if state._bets_this_sec < state.low_bps:
            state.low_bps = state._bets_this_sec
    if state._bets_this_min > 0:
        if state._bets_this_min > state.peak_bpm:
            state.peak_bpm = state._bets_this_min
        if state._bets_this_min < state.low_bpm:
            state.low_bpm = state._bets_this_min

    lo_bps = int(state.low_bps) if state.low_bps != float("inf") else 0
    lo_bpm = int(state.low_bpm) if state.low_bpm != float("inf") else 0

    game_label = GAMES.get(state.game, {}).get("label", state.game)
    pairs = [
        ("Session ID",      str(state.session_id)),
        ("Stop reason",     escape(state.status) if state.status else "-"),
        ("Platform",        f"Stake.com -- {game_label}"),
        ("Total Bets",      str(state.total_bets)),
        ("Wins / Losses",   f"{state.wins} / {state.losses}"),
        ("Win Rate",        f"{state.wins/state.total_bets*100:.1f}%" if state.total_bets else "--"),
        ("Profit",          f"{state.profit:+.8f} {state.currency.upper()}"),
        ("Start Balance",   f"{state.start_balance:.8f} {state.currency.upper()}"),
        ("Final Balance",   f"{state.current_balance:.8f} {state.currency.upper()}"),
        ("Streaks",         f"W+{state.max_win_streak}  L-{state.max_loss_streak}"),
        ("Best Win",        f"+{state.highest_win:.8f}"),
        ("Worst Loss",      f"-{state.biggest_loss:.8f}"),
        ("Speed",           f"{state.bets_per_second:.1f}/s  {state.bets_per_minute:.0f}/m  "
                            f"Range: {lo_bps}-{int(state.peak_bps)}/s  "
                            f"{lo_bpm}-{int(state.peak_bpm)}/m"),
    ]
    for k, v in pairs:
        sty = "green" if ("profit" in k.lower() and state.profit >= 0) else (
              "red"   if ("profit" in k.lower() and state.profit <  0) else "white")
        console.print(f"  [dim]{k:18s}[/] [{sty}]{v}[/]")
    console.print()

# ===========================================================
#  EXPORT + BACKTEST  (wolfbet v2.20.0 / v2.21.0)
# ===========================================================
def _read_session(session_id: int) -> Optional[dict]:
    conn = _db_conn()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _read_bets(session_id: int) -> list:
    conn = _db_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM bets WHERE session_id = ? ORDER BY id",
                            (session_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _latest_session_id() -> int:
    conn = _db_conn()
    try:
        row = conn.execute("SELECT MAX(id) FROM sessions").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] else 0


def _export_session(session_id: int, path: str) -> tuple:
    """Write a session and its bets to `path` (mode 0600). Returns (bet_count, format).

    A `.json` path writes {"session", "bets"}; anything else writes CSV with
    the session as leading `# key: value` lines. Raises ValueError when the
    session does not exist.
    """
    path = os.path.expanduser(path)
    fmt = "json" if path.lower().endswith(".json") else "csv"
    session = _read_session(session_id)
    if not session:
        raise ValueError(f"Session #{session_id} not found")
    bets = _read_bets(session_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as f:
        if fmt == "json":
            json.dump({"session": session, "bets": bets}, f, indent=2, default=str)
        else:
            for k, v in session.items():
                f.write(f"# {k}: {v}\n")
            if bets:
                w = csv.DictWriter(f, fieldnames=list(bets[0].keys()))
                w.writeheader()
                w.writerows(bets)
    return len(bets), fmt


def _simulate_bet_outcome(rng) -> dict:
    """One virtual bet at the current bet size, from stake's 1% house edge.

    Win chance is 99 / multiplier_target percent, the formula the wizard and
    the dice target use. Limbo draws a crash multiplier 0.99 / U; dice rolls
    0-100 against dice_target. Same shape as the game parsers return.
    """
    amount, target = state.current_bet, state.multiplier_target
    if state.game == "dice":
        roll = rng.random() * 100.0
        is_win = roll > state.dice_target if state.dice_condition == "above" else roll < state.dice_target
        value, display = roll, f"{roll:.2f}"
    else:
        value = 0.99 / max(rng.random(), 1e-12)
        is_win = value >= target
        display = f"{value:.4f}x"
    return {"amount": amount, "payout": amount * target if is_win else 0.0,
            "is_win": is_win, "result_value": value, "result_display": display}


def _replay_bets_from_db(session_id: int):
    """Yield a stored session's outcomes, sized and paid by the current strategy."""
    saved = _read_bets(session_id)
    if not saved:
        raise ValueError(f"Session #{session_id} has no bets to replay")

    def _gen():
        for hist in saved:
            amount = state.current_bet
            is_win = hist.get("state") == "win"
            yield {"amount": amount,
                   "payout": amount * state.multiplier_target if is_win else 0.0,
                   "is_win": is_win,
                   "result_value": hist.get("result_value") or 0.0,
                   "result_display": hist.get("result_display") or ""}
    return _gen()


def backtest_loop(source, progress_every: int = 100) -> None:
    """Run the strategy over `source` with no API call and no DB write.

    Uses the live loop's stop check, outcome bookkeeping, rules, milestones
    and next-bet code. A stop ends the backtest (no recurring restart).
    """
    for result in source:
        if not state.running:
            break
        stop, reason = should_stop()
        if stop:
            state.status = f"[BACKTEST] STOPPED: {reason}"
            break
        with state.lock:
            bet_state, _, _ = _record_bet_outcome(result["amount"], result["payout"],
                                                  result["is_win"])
            _check_milestone(bet_state)
        if state.strategy_key == "7" and state.custom_rules:
            apply_rules(bet_state)
        state.current_bet = _next_bet_after(bet_state)
        state.status = (f"[BACKTEST] bet {state.total_bets}: {bet_state}  "
                        f"bal {state.current_balance:.8f}  p/l {state.profit:+.8f}")
        if progress_every > 0 and state.total_bets % progress_every == 0:
            console.print(f"  [dim]BACKTEST[/] {state.total_bets} bets  "
                          f"W:{state.wins} L:{state.losses}  P/L: {state.profit:+.8f}  "
                          f"bal: {state.current_balance:.8f}")


def _reset_backtest_session() -> None:
    """In-memory session on a 1.0 sandbox balance; nothing is written to the DB."""
    s = state
    s.session_id, s.session_start = -1, time.time()
    s.running, s.paused, s.status = True, False, ""
    s.total_bets = s.wins = s.losses = 0
    s.profit = s.wagered = 0.0
    s.current_streak = s.max_win_streak = s.max_loss_streak = s.last_broken_loss_streak = 0
    s.highest_bet = s.highest_win = s.biggest_loss = 0.0
    s.start_balance = s.current_balance = s.highest_balance = s.lowest_balance = 1.0
    s.current_bet = s.base_bet
    s.dalembert_unit = s.paroli_count = 0
    s._last_profit_milestone = 0.0
    s.next_profit_milestone = s.profit_threshold or 0.0
    s.profit_increment_scope = "session"
    s.lanes_count, s.lane_split = 1, "none"


def _run_backtest(args) -> int:
    """--backtest: load a preset / the saved config / defaults, then simulate or replay.

    Never builds an HTTP client, solves Cloudflare or starts a browser.
    """
    console.print("[bold cyan]BACKTEST MODE[/]  [dim]no API calls, no real balance at risk[/]\n")
    init_db()
    if args.preset:
        if not load_preset(args.preset):
            console.print(f"[red]Preset '{escape(args.preset)}' not found.[/]")
            return 1
        console.print(f"  [dim]Loaded preset:[/] [cyan]{escape(args.preset)}[/]")
    elif load_config(rebuild_http=False):
        console.print(f"  [dim]Loaded saved config from[/] [cyan]{CONFIG_PATH}[/]")
    else:
        console.print("  [dim]Using backtest defaults (Martingale, limbo 2x).[/]")
        state.strategy, state.strategy_key = "Martingale", "2"
        state.game, state.multiplier_target = "limbo", 2.0
        state.base_bet, state.loss_mult, state.win_mult = MIN_BET, 2.0, 1.0
        state.currency = "usdt"
    _reset_backtest_session()
    if state.custom_rules_text:
        state.custom_rules = _load_rules_from_text(state.custom_rules_text)

    if args.replay_session:
        try:
            source = _replay_bets_from_db(args.replay_session)
        except ValueError as e:
            console.print(f"[red]{escape(str(e))}[/]")
            return 1
        console.print(f"  [dim]Replaying session[/] [cyan]#{args.replay_session}[/]\n")
        n = 1000
    else:
        n = args.simulate or 1000
        rng = random.Random(args.seed)
        seed_str = f"seed={args.seed}" if args.seed is not None else "seed=system-entropy"
        console.print(f"  [dim]Simulating[/] [cyan]{n}[/] [dim]bets with[/] [cyan]{seed_str}[/]\n")
        source = (_simulate_bet_outcome(rng) for _ in range(n))

    console.print(f"  [dim]Strategy:[/] [bold]{escape(state.strategy)}[/]  "
                  f"[dim]game:[/] {state.game}  [dim]base_bet:[/] {state.base_bet:.8f}  "
                  f"[dim]multiplier:[/] {state.multiplier_target}x\n")
    backtest_loop(source, progress_every=max(100, n // 20))
    if state.status.startswith("[BACKTEST] bet"):
        state.status = f"[BACKTEST] complete: {state.total_bets} bets"
    _print_summary()
    return 0


def cmd_export(path: str, session_id: Optional[int] = None) -> int:
    """--export: write one session (default: the latest) to PATH."""
    init_db()
    sid = session_id or _latest_session_id()
    if sid <= 0:
        console.print("[red]No sessions in the DB yet.[/]")
        return 1
    try:
        n, fmt = _export_session(sid, path)
    except (OSError, ValueError, sqlite3.Error) as e:
        console.print(f"[red]Export failed:[/] {escape(str(e))}")
        if "not found" in str(e).lower():
            console.print("[dim]  Tip: run `stake.py --stats` to see recent session IDs.[/]")
        return 1
    console.print(f"[green]OK[/] Exported [bold]{n}[/] bets from session #{sid} -> "
                  f"[cyan]{escape(os.path.expanduser(path))}[/] ({fmt.upper()})")
    return 0


# ===========================================================
#  CLI COMMANDS
# ===========================================================

# ── cmd_last_bets ─────────────────────────────────────────
def cmd_last_bets(n: int):
    """Show last N bets from database."""
    init_db()
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()
    rows = conn.execute("""
        SELECT b.id, b.session_id, substr(b.timestamp,1,19) AS dt,
               COALESCE(b.game, 'limbo') AS game,
               b.amount, b.result_value, COALESCE(b.result_display, '') AS rd,
               b.state, b.profit, b.balance_after
        FROM bets b ORDER BY b.id DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]No bets found.[/]")
        return

    console.print(Rule(f"[bold cyan]Last {len(rows)} Bets[/]"))
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("Bet",      width=7,  justify="right")
    t.add_column("Sess",     width=5,  justify="center")
    t.add_column("DateTime", width=19)
    t.add_column("Game",     width=6)
    t.add_column("Amount",   width=14, justify="right")
    t.add_column("Result",   width=10, justify="right")
    t.add_column("W/L",      width=4,  justify="center")
    t.add_column("P/L",      width=15, justify="right")
    t.add_column("Balance",  width=15, justify="right")

    for r in rows:
        bid, sid, dt, game, amt, roll, rd, st, pnl, bal = r
        rc = "bold green" if st == "win" else "bold red"
        pcc = "green" if (pnl or 0) >= 0 else "red"
        pss = "+" if (pnl or 0) >= 0 else ""
        result_str = rd if rd else (f"{roll:.2f}" if roll else "?")
        game_label = GAMES.get(game, {}).get("label", game or "?")
        t.add_row(
            str(bid), str(sid), dt or "?",
            game_label,
            f"{amt:.8f}" if amt else "?",
            result_str,
            Text("W" if st == "win" else "L", style=rc),
            Text(f"{pss}{pnl:.8f}" if pnl is not None else "?", style=pcc),
            f"{bal:.8f}" if bal else "?"
        )
    console.print(t)
    console.print()


# ── _compute_streak_distribution ──────────────────────────
def _compute_streak_distribution(conn, session_id: int) -> dict:
    """Compute streak distribution from bets table.
    Returns {"win": {streak_len: count, ...}, "loss": {streak_len: count, ...}}"""
    rows = conn.execute(
        "SELECT state FROM bets WHERE session_id=? ORDER BY id", (session_id,)
    ).fetchall()
    dist = {"win": {}, "loss": {}}
    if not rows:
        return dist
    cur_type = None
    cur_len = 0
    for (st,) in rows:
        kind = "win" if st == "win" else "loss"
        if kind == cur_type:
            cur_len += 1
        else:
            if cur_type and cur_len > 0:
                dist[cur_type][cur_len] = dist[cur_type].get(cur_len, 0) + 1
            cur_type = kind
            cur_len = 1
    if cur_type and cur_len > 0:
        dist[cur_type][cur_len] = dist[cur_type].get(cur_len, 0) + 1
    return dist


# ── cmd_session_bets ──────────────────────────────────────
def cmd_session_bets(session_id: int):
    """Show full detailed stats and bets for a specific session."""
    init_db()
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()

    sess = conn.execute("""
        SELECT id, started_at, ended_at,
               UPPER(currency), COALESCE(game, 'limbo'), strategy, base_bet, multiplier,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0)
        FROM sessions WHERE id = ?
    """, (session_id,)).fetchone()

    if not sess:
        console.print(f"[red]Session #{session_id} not found.[/]")
        conn.close()
        return

    sid, started, ended, cur, game, strat, base, mult, bets, wins, losses, \
        profit, wagered, start_bal, end_bal, mws, mls, \
        hi_bal, lo_bal, hi_win, big_loss, \
        bpm, bps, s_pk_bps, s_lw_bps, s_pk_bpm, s_lw_bpm = sess

    bets = bets or 0; wins = wins or 0; losses = losses or 0
    profit = profit or 0; wagered = wagered or 0
    start_bal = start_bal or 0; end_bal = end_bal or 0
    mws = mws or 0; mls = mls or 0; mult = mult or 0; base = base or 0
    hi_bal = hi_bal or 0; lo_bal = lo_bal or 0
    hi_win = hi_win or 0; big_loss = big_loss or 0
    bpm = bpm or 0; bps = bps or 0
    s_pk_bps = int(s_pk_bps or 0); s_lw_bps = int(s_lw_bps or 0)
    s_pk_bpm = int(s_pk_bpm or 0); s_lw_bpm = int(s_lw_bpm or 0)

    pc = "green" if profit >= 0 else "red"
    ps = "+" if profit >= 0 else ""
    wr = f"{wins/bets*100:.1f}%" if bets > 0 else "--"
    avg_pnl = profit / bets if bets > 0 else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    game_label = GAMES.get(game, {}).get("label", game or "?")

    # compute uptime
    uptime_str = "--"
    try:
        t0 = datetime.fromisoformat(started) if started else None
        t1 = datetime.fromisoformat(ended) if ended else datetime.now()
        if t0:
            delta = int((t1 - t0).total_seconds())
            h, rem = divmod(max(delta, 0), 3600)
            m, s = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"
    except Exception:
        pass

    console.print(Rule(f"[bold cyan]Session #{sid} -- Full Stats[/]"))
    console.print(f"  [dim]Started:[/]       {(started or '?').replace('T', ' ')}")
    console.print(f"  [dim]Ended:[/]         {(ended or 'running').replace('T', ' ')}")
    console.print(f"  [dim]Uptime:[/]        {uptime_str}")
    console.print(f"  [dim]Game:[/]          {game_label}")
    console.print(f"  [dim]Currency:[/]      {cur}")
    console.print(f"  [dim]Strategy:[/]      {strat}  {mult}x  Base: {base:.8f}")
    console.print(f"  [dim]Bets:[/]          {bets}  [dim]W:[/] [green]{wins}[/]  [dim]L:[/] [red]{losses}[/]  [dim]WR:[/] {wr}")
    console.print(f"  [dim]Profit:[/]        [{pc}]{ps}{profit:.8f}[/]")
    console.print(f"  [dim]Wagered:[/]       {wagered:.8f}")
    console.print(f"  [dim]Avg P/L:[/]       [{avg_c}]{avg_s}{avg_pnl:.8f}[/] per bet")
    console.print(f"  [dim]Balance:[/]       {start_bal:.8f} -> {end_bal:.8f}")
    console.print(f"  [dim]Peak / Low:[/]    [green]{hi_bal:.8f}[/] / [red]{lo_bal:.8f}[/]")
    console.print(f"  [dim]Best Streaks:[/]  W+{mws}  L-{mls}")
    console.print(f"  [dim]Best Win:[/]      [green]+{hi_win:.8f}[/]")
    console.print(f"  [dim]Worst Loss:[/]    [red]-{big_loss:.8f}[/]")
    console.print(f"  [dim]Speed:[/]         {bps:.1f} avg/s  {bpm:.0f} avg/m  Range: {s_lw_bps}-{s_pk_bps}/s  {s_lw_bpm}-{s_pk_bpm}/m")

    # Streak Distribution
    console.print()
    console.print(Rule("[bold cyan]Streak Distribution[/]"))

    dist = _compute_streak_distribution(conn, session_id)

    loss_dist = dist["loss"]
    if loss_dist:
        console.print(f"  [bold red]Loss Streaks[/]  (how many times each streak length occurred)")
        t_loss = Table(box=box.SIMPLE, show_edge=False, padding=(0, 2))
        t_loss.add_column("Streak", style="bold red", justify="right")
        t_loss.add_column("Count", justify="right")
        t_loss.add_column("", width=30)
        max_count = max(loss_dist.values()) if loss_dist else 1
        for length in sorted(loss_dist.keys()):
            count = loss_dist[length]
            bar_w = int(count / max_count * 25)
            bar = "[red]" + "\u2588" * bar_w + "[/]"
            t_loss.add_row(f"L-{length}", str(count), bar)
        console.print(t_loss)
    else:
        console.print("  [dim]No loss streaks recorded[/]")

    console.print()

    win_dist = dist["win"]
    if win_dist:
        console.print(f"  [bold green]Win Streaks[/]  (how many times each streak length occurred)")
        t_win = Table(box=box.SIMPLE, show_edge=False, padding=(0, 2))
        t_win.add_column("Streak", style="bold green", justify="right")
        t_win.add_column("Count", justify="right")
        t_win.add_column("", width=30)
        max_count = max(win_dist.values()) if win_dist else 1
        for length in sorted(win_dist.keys()):
            count = win_dist[length]
            bar_w = int(count / max_count * 25)
            bar = "[green]" + "\u2588" * bar_w + "[/]"
            t_win.add_row(f"W+{length}", str(count), bar)
        console.print(t_win)
    else:
        console.print("  [dim]No win streaks recorded[/]")

    # Recent Bets
    rows = conn.execute("""
        SELECT id, substr(timestamp,12,8), COALESCE(game, 'limbo'),
               amount, result_value, COALESCE(result_display, ''),
               state, profit, balance_after
        FROM bets WHERE session_id = ? ORDER BY id DESC LIMIT 50
    """, (session_id,)).fetchall()
    conn.close()

    if rows:
        console.print()
        console.print(Rule(f"[bold cyan]Last {len(rows)} Bets[/]"))
        t = Table(box=box.SIMPLE, expand=True)
        t.add_column("Bet",     width=7, justify="right")
        t.add_column("Time",    width=9)
        t.add_column("Game",    width=6)
        t.add_column("Amount",  width=14, justify="right")
        t.add_column("Result",  width=10, justify="right")
        t.add_column("W/L",     width=4, justify="center")
        t.add_column("P/L",     width=15, justify="right")
        t.add_column("Balance", width=15, justify="right")

        for r in rows:
            bid, tm, g, amt, roll, rd, st, pnl, bal = r
            rc = "bold green" if st == "win" else "bold red"
            pcc = "green" if (pnl or 0) >= 0 else "red"
            pss = "+" if (pnl or 0) >= 0 else ""
            result_str = rd if rd else (f"{roll:.2f}" if roll else "?")
            gl = GAMES.get(g, {}).get("label", g or "?")
            t.add_row(
                str(bid), tm or "?",
                gl,
                f"{amt:.8f}" if amt else "?",
                result_str,
                Text("W" if st == "win" else "L", style=rc),
                Text(f"{pss}{pnl:.8f}" if pnl is not None else "?", style=pcc),
                f"{bal:.8f}" if bal else "?"
            )
        console.print(t)
    console.print()


# ── cmd_monitor ───────────────────────────────────────────
def _monitor_read_state():
    """Read daemon state file and return dict, or None if not running."""
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return None

def _monitor_get_pid(d: dict) -> int:
    """Get daemon PID and verify it's alive."""
    pid = d.get("pid", 0)
    if pid:
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            pass
    return 0

def _monitor_get_recent_bets(session_id: int, limit: int) -> list:
    """Fetch recent bets from DB for the monitor view."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = _db_conn()
        rows = conn.execute("""
            SELECT id, substr(timestamp,1,19), COALESCE(game, 'limbo'),
                   amount, result_value, COALESCE(result_display, ''),
                   state, profit, balance_after
            FROM bets WHERE session_id=? ORDER BY id DESC LIMIT ?
        """, (session_id, limit)).fetchall()
        conn.close()
        result = []
        for r in rows:
            result.append({
                "n": r[0], "time": (r[1] or "")[11:19],
                "game": r[2] or "limbo",
                "amt": r[3] or 0, "roll": r[4] or 0,
                "rd": r[5] or "",
                "state": r[6] or "lose", "pnl": r[7] or 0,
                "bal": r[8] or 0,
            })
        return result
    except Exception:
        return []

def _build_monitor_screen(d: dict, bets: list, pid_alive: bool) -> str:
    """Build full-screen ANSI dashboard from state dict + bet list."""
    try:
        term_size = os.get_terminal_size()
        term_h = term_size.lines
        term_w = term_size.columns
    except OSError:
        term_h, term_w = 24, 80

    lines = []
    def add(s: str):
        lines.append(f"{s}{_CLR_EOL}")
    def add_blank():
        lines.append(_CLR_EOL)

    # Extract fields with safe defaults
    sid       = d.get("session_id", "?")
    uptime    = str(timedelta(seconds=d.get("uptime_sec", 0)))
    currency  = d.get("currency", "?").upper()
    strategy  = d.get("strategy", "?")
    mult      = d.get("multiplier_target", 0)
    game      = d.get("game", "limbo")
    base_bet  = d.get("base_bet", 0)
    cur_bet   = d.get("current_bet", 0)
    hi_bet    = d.get("highest_bet", 0)
    bal       = d.get("current_balance", 0)
    start_bal = d.get("start_balance", 0)
    profit    = d.get("profit", 0)
    wagered   = d.get("wagered", 0)
    total     = d.get("total_bets", 0)
    wins      = d.get("wins", 0)
    losses    = d.get("losses", 0)
    cs        = d.get("current_streak", 0)
    mws       = d.get("max_win_streak", 0)
    mls       = d.get("max_loss_streak", 0)
    hi_bal    = d.get("highest_balance", 0) or bal
    lo_bal    = d.get("lowest_balance", 0) or bal
    hi_win    = d.get("highest_win", 0)
    big_loss  = d.get("biggest_loss", 0)
    bps       = d.get("bets_per_second", 0)
    bpm       = d.get("bets_per_minute", 0)
    pk_bps    = int(d.get("peak_bps", 0))
    lw_bps    = int(d.get("low_bps", 0))
    pk_bpm    = int(d.get("peak_bpm", 0))
    lw_bpm    = int(d.get("low_bpm", 0))
    paused    = d.get("paused", False)
    status    = d.get("status", "")

    # Game info string
    game_label = GAMES.get(game, {}).get("label", game)
    if game == "dice":
        game_info = f"Dice {d.get('dice_condition', 'above')} {d.get('dice_target', 50.5)}  {mult}x"
    else:
        game_info = f"Limbo {mult}x target"

    # Row 1: Header
    if pid_alive and int(d.get("pause_remaining_sec", 0) or 0) > 0:
        st_ind = _a("bold_cyan", _pause_label(d))
    elif pid_alive and not paused:
        st_ind = _a("bold_green", "* LIVE")
    elif pid_alive and paused:
        st_ind = _a("bold_yellow", "* PAUSED")
    else:
        st_ind = _a("bold_red", "* STOPPED")

    run_seg = (f"  Run {timedelta(seconds=int(d['run_uptime_sec']))}"
               if d.get("run_uptime_sec") is not None else "")
    add(f"{_a('dim_magenta', '--- ')}{_a('bold_cyan', f'Stake v{VERSION}')}"
        f"{_a('white', f'  #{sid}  {uptime}{run_seg}')}  {st_ind} "
        f"{_a('dim', f'  {game_info}')}"
        f"{_a('dim_magenta', ' ' + '-' * max(0, term_w - 80))}")

    # Row 2: Balance bar
    ps = "+" if profit >= 0 else ""
    pc = "bold_green" if profit >= 0 else "bold_red"
    wr = (wins / total * 100) if total else 0
    wr_c = "bold_green" if wr >= 50 else "bold_red"
    add(f"{_COLORS['bg_magenta']}{_BOLD}{_COLORS['white']} BAL {_RST} "
        f"{_a('bold_white', f'{bal:.8f} {currency}')}"
        f"  {_a('dim', 'PnL')} {_a(pc, f'{ps}{profit:.8f}')}"
        f"  {_a('dim', 'WAG')} {_a('yellow', f'{wagered:.8f}')}"
        f"  {_a('dim', 'WR')} {_a(wr_c, f'{wr:.1f}%')}")

    # Row 3: Stats
    if cs > 0:
        sk = _a("green", f"W+{cs}")
    elif cs < 0:
        sk = _a("red", f"L{cs}")
    else:
        sk = _a("dim", "--")
    add(f" {_a('dim', 'Bets')} {_a('bold_white', str(total))}"
        f"  {_a('green', 'W')} {_a('bold_green', str(wins))}"
        f"  {_a('red', 'L')} {_a('bold_red', str(losses))}"
        f"  {_a('dim', 'Str')} {sk}"
        f"  {_a('dim', f'Best W+{mws}/L-{mls}')}")

    # Row 4: Bet / Strategy / Speed
    s4 = f" {_a('dim', 'Bet')} {_a('yellow', f'{cur_bet:.8f}')}"
    s4 += f"  {_a('dim', 'Base')} {_a('bold_green', f'{base_bet:.8f}')}"
    s4 += (f"  {_a('dim', 'Hi')} {_a('white', f'{hi_bet:.8f}')}"
           f"  {_a('cyan', strategy)}"
           f"  {_a('dim', f'{mult}x')}"
           f"  {_a('dim', 'BPS')} {_a('magenta', f'{bps:.1f}')}"
           f"  {_a('dim', 'BPM')} {_a('magenta', f'{bpm:.0f}')}"
           f"  {_a('dim', 'Speed')} {_a('magenta', f'{lw_bps}-{pk_bps}/s {lw_bpm}-{pk_bpm}/m')}")
    add(s4)

    # Row 5: Extremes / Performance
    avg_pnl = profit / total if total else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    bal_change = bal - start_bal
    bc_c = "bold_green" if bal_change >= 0 else "bold_red"
    bc_s = "+" if bal_change >= 0 else ""
    s5 = (f" {_a('dim', 'Peak')} {_a('green', f'{hi_bal:.8f}')}"
          f"  {_a('dim', 'Low')} {_a('red', f'{lo_bal:.8f}')}"
          f"  {_a('dim', 'BestW')} {_a('green', f'+{hi_win:.8f}')}"
          f"  {_a('dim', 'WorstL')} {_a('red', f'-{big_loss:.8f}')}"
          f"  {_a('dim', 'Avg')} {_a(avg_c, f'{avg_s}{avg_pnl:.8f}')}"
          f"  {_a('dim', 'Bal')} {_a(bc_c, f'{bc_s}{bal_change:.8f}')}")
    add(s5)

    # Row 6: Sparkline placeholder
    add(_build_config_progress_row(d))

    # Row 7: Separator
    add(_a("dim_magenta", "-" * term_w))

    # Row 8: Bets table header
    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Game':<6} {'Amount':>14}  {'Result':>10}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    # Rows 9..N-3: Recent bet rows (3 footer rows below)
    bets_avail = max(0, term_h - 11)
    shown = bets[:bets_avail]

    for b in shown:
        bn = b["n"]
        btm = b["time"]
        bgame = GAMES.get(b["game"], {}).get("label", b["game"])[:6]
        bamt = b["amt"]
        broll = b["roll"]
        brd = b["rd"]
        bpnl = b["pnl"]
        bbal = b["bal"]
        bwl_c = "bold_green" if b["state"] == "win" else "bold_red"
        bwl_t = " W " if b["state"] == "win" else " L "
        bpnl_c = "green" if bpnl >= 0 else "red"
        bsign = "+" if bpnl >= 0 else ""
        result_str = brd if brd else f"{broll:>10.2f}"
        add(f"{_a('dim', f'{bn:>6}')}"
            f"  {_a('dim', f'{btm:<9}')}"
            f" {_a('dim', f'{bgame:<6}')}"
            f" {_a('white', f'{bamt:>14.8f}')}"
            f"  {_a('white', f'{result_str:>10}')}"
            f"  {_a(bwl_c, bwl_t)}"
            f"  {_a(bpnl_c, f'{bsign}{bpnl:>14.8f}')}"
            f"  {_a('white', f'{bbal:>14.8f}')}")

    for _ in range(bets_avail - len(shown)):
        add_blank()

    # Row N-1: Bottom separator
    add(_a("dim_magenta", "-" * term_w))

    # Row N: Status + controls
    status_txt = status[:60] if status else "Monitoring..."
    keys = "[P]ause [R]esume [Q]uit/Detach [S]top daemon"
    add(f" {_a('bold_white', 'MONITOR')} {_a('white', status_txt)}  {_a('dim', keys)}")
    # v1.12.0 (WolfBet v2.28.1): T / H are foreground-TUI only; show the daemon paths.
    add(f" {_a('dim', 'Tweak config:')} {_a('cyan', 'stake.py --config-set KEY=VALUE')}   "
        f"{_a('dim', '· History:')} {_a('cyan', 'stake.py --stats / --analytics')}")

    return "\r\n".join(lines)


SYSTEMD_UNIT = "stake"   # stakectl's $SERVICE

def _service_unit() -> str:
    """systemd unit of this lane: stake (lane 1) or stake-lane@K (v1.13.0)."""
    return SYSTEMD_UNIT if state.lane <= 1 else f"{SYSTEMD_UNIT}-lane@{state.lane}"

def _is_systemd_managed(pid) -> bool:
    """True when pid runs inside the stake systemd unit (Linux /proc cgroup check).

    The monitor's stop then goes through systemctl, so the unit's Restart=
    policy does not respawn the bot after a clean SIGTERM exit.
    """
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return f"/{_service_unit()}.service" in f.read()
    except OSError:
        return False


def _stop_daemon(pid) -> str:
    """Stop the daemon: `systemctl --user stop` when systemd manages it, else SIGTERM."""
    if _is_systemd_managed(pid):
        try:
            import subprocess as _sp
            _sp.run(["systemctl", "--user", "stop", _service_unit()], check=False, timeout=10)
            return "systemd"
        except Exception:
            pass
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            return "signal"
        except OSError:
            pass
    return ""


def cmd_monitor():
    """Attach a live TUI viewer to a running daemon session."""
    d = _monitor_read_state()
    if not d:
        console.print("[yellow]No running session found. Start with: python3 stake.py --daemon[/]")
        return
    pid = _monitor_get_pid(d)
    if not pid:
        console.print("[yellow]Daemon not running (stale state file).[/]")
        return

    sid = d.get("session_id", 0)
    console.print(f"[dim]Attaching to session #{sid} (PID {pid})... Press Q to detach.[/]")
    time.sleep(0.5)

    # Save terminal state
    _old_term = None
    try:
        import termios
        _old_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    # Enter alt screen
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()

    # Keyboard reader in background thread
    _monitor_running = [True]
    _monitor_action = [None]

    def _monitor_keys():
        while _monitor_running[0]:
            try:
                import termios as _t, tty as _tty
                fd = sys.stdin.fileno()
                old = _t.tcgetattr(fd)
                try:
                    _tty.setraw(fd)
                    ch = sys.stdin.read(1).lower()
                finally:
                    _t.tcsetattr(fd, _t.TCSADRAIN, old)
                if ch in ("q", "\x03", "\x1c"):
                    _monitor_action[0] = "quit"
                    _monitor_running[0] = False
                elif ch == "p":
                    _monitor_action[0] = "pause"
                elif ch == "r":
                    _monitor_action[0] = "resume"
                elif ch == "s":
                    _monitor_action[0] = "stop"
                    _monitor_running[0] = False
            except Exception:
                time.sleep(0.1)

    t_keys = threading.Thread(target=_monitor_keys, daemon=True)
    t_keys.start()

    try:
        while _monitor_running[0]:
            action = _monitor_action[0]
            _monitor_action[0] = None
            if action == "pause" and pid:
                try:
                    os.kill(pid, signal.SIGUSR1)
                except OSError:
                    pass
            elif action == "resume" and pid:
                try:
                    os.kill(pid, signal.SIGUSR2)
                except OSError:
                    pass

            d = _monitor_read_state()
            if not d:
                break
            pid = _monitor_get_pid(d)

            bet_limit = max(0, (os.get_terminal_size().lines if sys.stdout.isatty() else 24) - 10)
            recent_bets = _monitor_get_recent_bets(sid, bet_limit)

            sys.stdout.write("\033[H")
            sys.stdout.write(_build_monitor_screen(d, recent_bets, bool(pid)))
            sys.stdout.flush()
            time.sleep(0.5)

        if _monitor_action[0] == "stop" or (locals().get("action") == "stop"):
            _stop_daemon(pid)
    finally:
        _monitor_running[0] = False
        sys.stdout.write("\033[?25h\033[?1049l\033[0m")
        sys.stdout.flush()
        if _old_term is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _old_term)
            except Exception:
                pass
        import subprocess
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
        except Exception:
            pass

    if _monitor_action[0] == "stop":
        console.print("[bold red]Daemon stopped.[/]")
    else:
        console.print("[dim]Detached from monitor. Daemon continues running.[/]")


# ── cmd_status ────────────────────────────────────────────
def _read_lane_states() -> list:
    """[(lane, state-dict | None, alive)] for every registry lane; a lane that never ran (no
    state file) is listed as not alive, so a lane that should bet but doesn't is still seen."""
    out = []
    for k in lane_ids(_read_config_raw() or {}):
        path = lane_path(_BASE_STATE_PATH, k)
        if not os.path.exists(path):
            out.append((k, None, False))
            continue
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            out.append((k, None, False))
            continue
        alive = False
        try:
            if d.get("pid"):
                os.kill(int(d["pid"]), 0)
                alive = True
        except (OSError, ValueError, TypeError):
            pass
        out.append((k, d, alive))
    return out


def cmd_lanes_status():
    """v1.13.0 (wolfbet v2.35.0) -- one row per lane + totals. No Pace column:
    Stake sends no rate-limit quota headers, so there is no soft pacing."""
    rows = _read_lane_states()
    if not rows:
        console.print("[yellow]No lane state files found.[/]")
        return
    names = {lane: entry["name"] for lane, entry in lane_registry(_read_config_raw() or {}).items()
             if entry["name"] != default_lane_name(lane)}
    t = Table(title="Parallel lanes", title_style="bold cyan", box=box.SIMPLE)
    cols = ("Lane",) + (("Name",) if names else ()) + (
        "State", "Session", "Uptime", "Run", "Profit", "Equity", "bps", "Code")
    for col in cols:
        t.add_column(col, justify="left" if col in ("Name", "State") else "right")
    tot_bps = tot_profit = 0.0
    for k, d, alive in rows:
        lead = (str(k), escape(names.get(k, ""))) if names else (str(k),)
        if d is None:
            never = not os.path.exists(lane_path(_BASE_STATE_PATH, k))
            t.add_row(*lead, "[red]NOT RUNNING[/]" if never else "[red]unreadable[/]", *["-"] * 7)
            continue
        p = float(d.get("profit", 0) or 0)
        bps = float(d.get("bets_per_second", 0) or 0) if alive else 0.0
        eq = d.get("lane_equity")
        tot_profit += p
        tot_bps += bps
        st = "[green]LIVE[/]" if alive else "[red]STOPPED[/]"
        if alive and int(d.get("pause_remaining_sec", 0) or 0) > 0:
            st = "[cyan]PAUSED[/]"
        run = d.get("run_uptime_sec")
        t.add_row(*lead, st, f"#{d.get('session_id', '?')}",
                  str(timedelta(seconds=int(d.get("uptime_sec", 0) or 0))),
                  fmt_duration(run) if run is not None else "-",
                  f"[{'green' if p >= 0 else 'red'}]{p:+.8f}[/]",
                  f"{eq:.8f}" if isinstance(eq, (int, float)) else "-",
                  f"{bps:.1f}", _lane_code(k, d) if alive else "-")
    console.print(t)
    console.print(f"  [dim]Total:[/] [bold]{tot_bps:.1f}[/] bps   "
                  f"[dim]session P/L[/] [{'green' if tot_profit >= 0 else 'red'}]{tot_profit:+.8f}[/]   "
                  f"[dim]lanes live[/] {sum(1 for _, _, a in rows if a)}/{len(rows)}")
    for k, d, alive in rows:
        pending = (d or {}).get("pending_restart") if alive else None
        if isinstance(pending, dict) and pending:
            parts = ", ".join(f"{key} {_fmt_config_value(key, v[0])} -> {_fmt_config_value(key, v[1])}"
                              for key, v in sorted(pending.items())
                              if isinstance(v, list) and len(v) == 2)
            console.print(f"  [yellow]Lane {k} waits for a restart:[/] {parts} "
                          f"[dim](stakectl restart {k})[/]")


def _lane_code(lane: int, d: Optional[dict]) -> str:
    """Which code a lane runs during an update: `new` (this installed version), `waiting for
    session end` (old code, a restart asked for), or `old`."""
    if d and d.get("code_version") == VERSION:
        return "new"
    try:
        with open(lane_path(DRAIN_PATH, lane)) as f:
            if f.read().strip() == "restart":
                return "waiting for session end"
    except OSError:
        pass
    return "old"


def cmd_status():
    if not os.path.exists(STATE_PATH):
        console.print("[yellow]No running session found.[/]")
        return
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception:
        console.print("[red]Could not read state file.[/]")
        return

    pid = d.get("pid", 0)
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass

    status = "[bold green]RUNNING[/]" if alive else "[bold red]STOPPED[/]"
    p = d.get("profit", 0)
    ps = "+" if p >= 0 else ""
    pc = "green" if p >= 0 else "red"
    bets = d.get("total_bets", 0)
    wins = d.get("wins", 0)
    losses = d.get("losses", 0)
    wr = (wins / bets * 100) if bets > 0 else 0
    uptime = str(timedelta(seconds=d.get("uptime_sec", 0)))
    hi_bal = d.get("highest_balance", 0)
    lo_bal = d.get("lowest_balance", 0)
    hi_win = d.get("highest_win", 0)
    big_loss = d.get("biggest_loss", 0)
    bpm = d.get("bets_per_minute", 0)
    bps = d.get("bets_per_second", 0)
    pk_bps = int(d.get("peak_bps", 0))
    lw_bps = int(d.get("low_bps", 0))
    pk_bpm = int(d.get("peak_bpm", 0))
    lw_bpm = int(d.get("low_bpm", 0))
    start_bal = d.get("start_balance", 0)
    cur_bal = d.get("current_balance", 0)
    avg_pnl = p / bets if bets > 0 else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    cs = d.get("current_streak", 0)
    if cs > 0:
        sk_now = f"W+{cs}"
    elif cs < 0:
        sk_now = f"L{cs}"
    else:
        sk_now = "--"

    # Game info string
    game = d.get("game", "limbo")
    game_label = GAMES.get(game, {}).get("label", game)
    if game == "dice":
        game_info = f"{game_label} {d.get('dice_condition', 'above')} {d.get('dice_target', 50.5)}"
    else:
        game_info = game_label

    console.print(Rule(f"[bold cyan]Stake Session #{d.get('session_id', '?')}  {status}[/]"))

    # Strategy string
    mult = d.get("multiplier_target", 0)
    strat_str = f"{d.get('strategy', '?')}  {mult}x  {game_info}"
    sk = d.get("strategy_key", "")
    if sk in ("2", "6", "7"):
        strat_str += f"  [dim](loss mult: {d.get('loss_mult', '?')}x)[/]"
    elif sk in ("3", "5"):
        strat_str += f"  [dim](win mult: {d.get('win_mult', '?')}x)[/]"

    uptime_txt = uptime
    if int(d.get("pause_remaining_sec", 0) or 0) > 0:
        left = timedelta(seconds=int(d["pause_remaining_sec"]))
        total = timedelta(seconds=int(d.get("pause_total_sec", 0) or 0))
        uptime_txt += f"  · Recurring pause: {left} remaining / {total} total"
    rows = [
        ("Uptime",         uptime_txt),
    ]
    if d.get("run_uptime_sec") is not None:
        rows.append(("Running for", str(timedelta(seconds=int(d["run_uptime_sec"])))))
    rows += [
        ("Game",           game_info),
        ("Currency",       d.get("currency", "?").upper()),
        ("Strategy",       strat_str),
        ("Base / Current", f"{d.get('base_bet', 0):.8f} / {d.get('current_bet', 0):.8f}"),
        ("Highest Bet",    f"{d.get('highest_bet', 0):.8f}"),
    ]

    delay = d.get("bet_delay", 0)
    if delay:
        rows.append(("Bet Delay",     f"{delay}s"))

    rows += [
        ("Balance",        f"{start_bal:.8f} → {cur_bal:.8f}"),
        ("Peak / Low",     f"[green]{hi_bal:.8f}[/] / [red]{lo_bal:.8f}[/]"),
        ("Profit",         f"[{pc}]{ps}{p:.8f}[/]"),
        ("Wagered",        f"{d.get('wagered', 0):.8f}"),
        ("Avg P/L",        f"[{avg_c}]{avg_s}{avg_pnl:.8f}[/] per bet"),
        ("Bets",           str(bets)),
        ("Wins / Losses",  f"[green]{wins}[/] / [red]{losses}[/]"),
        ("Win Rate",       f"{wr:.1f}%"),
        ("Current Streak", sk_now),
        ("Best Streaks",   f"W+{d.get('max_win_streak', 0)}  L-{d.get('max_loss_streak', 0)}"),
        ("Best Win",       f"[green]+{hi_win:.8f}[/]"),
        ("Worst Loss",     f"[red]-{big_loss:.8f}[/]"),
        ("Speed",          f"{bps:.1f}/s  {bpm:.0f}/m  Range: {lw_bps}-{pk_bps}/s  {lw_bpm}-{pk_bpm}/m"),
    ]

    # v1.12.0 -- observability from the state file (WolfBet v2.26.1 / v2.30.3)
    api_p50, api_p99, api_peak = d.get("api_p50_ms"), d.get("api_p99_ms"), d.get("api_peak_ms")
    if api_p50 or api_peak:
        rows.append(("API", f"p50 [cyan]{api_p50 or 0:.0f}ms[/]  · "
                            f"p99 [cyan]{api_p99 or 0:.0f}ms[/]  · "
                            f"peak [cyan]{api_peak or 0:.0f}ms[/]"))
    cycle_ms = d.get("cycle_ms")
    if cycle_ms:
        tmax = float(d.get("theoretical_max_bps") or 0)
        rows.append(("Cycle", f"[magenta]{cycle_ms:.0f}ms[/]/bet "
                              f"(overhead [cyan]{d.get('overhead_ms') or 0:.0f}ms[/])"
                              f"  -> [magenta]{tmax:.2f}[/] BPS ceiling"))
    eff_pct = d.get("efficiency_pct")
    if eff_pct is not None:
        eff_c = "green" if eff_pct >= 90 else "yellow"
        rows.append(("Efficiency", f"[{eff_c}]{eff_pct:.0f}%[/] of theoretical"))

    # Stop conditions
    stop_parts = []
    mp = d.get("max_profit")
    if mp is not None:
        stop_parts.append(f"Profit >= {mp}")
    ml = d.get("max_loss")
    if ml is not None:
        stop_parts.append(f"Loss >= {ml}")
    mb = d.get("max_bets")
    if mb is not None:
        stop_parts.append(f"Bets >= {mb}")
    mw = d.get("max_wins")
    if mw is not None:
        stop_parts.append(f"Wins >= {mw}")
    sb = d.get("stop_on_balance")
    if sb is not None:
        stop_parts.append(f"Balance <= {sb}")
    if stop_parts:
        rows.append(("Stop Conditions", " | ".join(stop_parts)))
    else:
        rows.append(("Stop Conditions", "[dim]None set[/]"))

    # Profit increment
    pi = d.get("profit_increment")
    pt = d.get("profit_threshold")
    if pi and pt:
        if d.get("profit_increment_scope") == "run":
            rows.append(("Profit Bump", f"+{pi:.8f} every {pt} run profit"))
            nxt = d.get("next_profit_milestone")
            rows.append(("Run Profit",
                         f"{float(d.get('run_profit_total') or 0):+.8f}  "
                         f"[dim](since #{d.get('run_first_session') or '?'}, "
                         f"{d.get('run_bumps') or 0} bump(s)"
                         + (f", next at {float(nxt):+.8f}" if nxt else "") + ")[/]"))
        else:
            rows.append(("Profit Bump", f"+{pi:.8f} every {pt} profit"))
    if d.get("recurring"):
        rows.append(("Recurring", f"on, {d.get('recurring_delay_sec', 0)}s pause"))
    if d.get("streak_reset_losses"):
        rows.append(("Streak Reset", f"{d['streak_reset_losses']} losses then win → "
                                     f"{d.get('streak_reset_delay_sec', 0)}s pause"))

    rows += [
        ("Last Status",    d.get("status", "")[:80]),
        ("Updated",        d.get("updated_at", "?").replace("T", " ")),
    ]

    for k, v in rows:
        console.print(f"  [dim]{k:18s}[/] {v}")
    if d.get("last_error"):
        console.print(f"  [dim]{'Last Error':18s}[/] [red]{d['last_error'][:60]}[/]")

    # Rule-Based rules
    rc = d.get("rule_count", 0)
    if rc > 0:
        console.print()
        console.print(f"  [yellow]Rules ({rc}):[/]")
        for i, desc in enumerate(d.get("rule_descriptions", []), 1):
            console.print(f"    [dim]{i}.[/] {desc}")

    console.print()

def cmd_stop():
    if not os.path.exists(STATE_PATH):
        console.print("[yellow]No running session found.[/]")
        return
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
        pid = d.get("pid", 0)
        if pid:
            os.kill(pid, signal.SIGTERM)
            console.print(f"[green]Sent SIGTERM to PID {pid}[/]")
        else:
            console.print("[yellow]No PID found in state file.[/]")
    except ProcessLookupError:
        console.print("[yellow]Process not running.[/]")
        _cleanup_state_file()
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Error{detail}.[/]")

def cmd_list_presets():
    presets = list_presets()
    if not presets:
        console.print("[dim]No saved presets.[/]")
        return
    for name, data in presets.items():
        game = data.get("game", "limbo")
        console.print(f"  [yellow]{name}[/]  [dim]->[/] {data.get('strategy', '?')} on "
                      f"{game}/{data.get('currency', '?').upper()} @ {data.get('base_bet', 0):.8f}  "
                      f"{data.get('multiplier_target', 0)}x")

def cmd_stats(since: str = None, until: str = None, lane: int = None):
    """Show session statistics, optionally filtered to a date window / lane."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    init_db()  # v1.11.0 -- adds sessions.lane on older DBs
    try:
        where, params, window_label = stats_date_window(since, until, lane)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return
    conn = _db_conn()
    limit = 50 if (since or until) else 20

    # session list - fetch all columns
    rows = conn.execute(f"""
        SELECT id, started_at AS started, ended_at AS ended,
               UPPER(currency) AS cur, COALESCE(game, 'limbo') AS game,
               strategy, multiplier, base_bet,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0),
               COALESCE(lane, 1)
        FROM sessions{where} ORDER BY id DESC LIMIT ?
    """, (*params, limit)).fetchall()

    if not rows:
        console.print("[dim]No sessions found.[/]")
        conn.close()
        return

    console.print(Rule(f"[bold cyan]Session History (last {limit})[/]{window_label}"))

    for r in rows:
        sid, started, ended, cur, game, strat, mult, base, bets, wins, losses, \
            profit, wagered, start_bal, end_bal, mws, mls, \
            hi_bal, lo_bal, hi_win, big_loss, \
            bpm, bps, s_peak_bps, s_low_bps, s_peak_bpm, s_low_bpm, s_lane = r
        lane_tag = f" [magenta]\\[L{s_lane}][/]" if s_lane and s_lane > 1 else ""
        bets = bets or 0
        wins = wins or 0
        losses = losses or 0
        profit = profit or 0
        wagered = wagered or 0
        start_bal = start_bal or 0
        end_bal = end_bal or 0
        mws = mws or 0
        mls = mls or 0
        mult = mult or 0
        base = base or 0
        hi_bal = hi_bal or 0
        lo_bal = lo_bal or 0
        hi_win = hi_win or 0
        big_loss = big_loss or 0
        bpm = bpm or 0
        bps = bps or 0
        s_peak_bps = int(s_peak_bps or 0)
        s_low_bps = int(s_low_bps or 0)
        s_peak_bpm = int(s_peak_bpm or 0)
        s_low_bpm = int(s_low_bpm or 0)
        pc = "green" if profit >= 0 else "red"
        ps = "+" if profit >= 0 else ""
        wr = f"{wins/bets*100:.1f}%" if bets > 0 else "--"
        avg_pnl = profit / bets if bets > 0 else 0
        avg_c = "green" if avg_pnl >= 0 else "red"
        avg_s = "+" if avg_pnl >= 0 else ""
        game_label = GAMES.get(game, {}).get("label", game or "?")

        # compute uptime
        uptime_str = ""
        try:
            t0 = datetime.fromisoformat(started) if started else None
            t1 = datetime.fromisoformat(ended) if ended else datetime.now()
            if t0:
                delta = int((t1 - t0).total_seconds())
                h, rem = divmod(max(delta, 0), 3600)
                m, s = divmod(rem, 60)
                uptime_str = f"  [dim]Uptime:[/] {h}h {m}m {s}s"
        except Exception:
            pass

        console.print(f"  [bold cyan]Session #{sid}[/]{lane_tag}  [dim]{(started or '?').replace('T', ' ')} -> {(ended or 'running').replace('T', ' ')}[/]{uptime_str}")
        console.print(f"    [dim]Game:[/] {game_label}  [dim]Currency:[/] {cur or '?'}  [dim]Strategy:[/] {strat or '?'}  [dim]Mult:[/] {mult}x  [dim]Base:[/] {base:.8f}")
        console.print(f"    [dim]Bets:[/] {bets}  [dim]W:[/] [green]{wins}[/]  [dim]L:[/] [red]{losses}[/]  [dim]WR:[/] {wr}")
        console.print(f"    [dim]Speed:[/] {bps:.1f} avg/s  {bpm:.0f} avg/m  [dim]BPS:[/] {s_low_bps}-{s_peak_bps}/s  [dim]BPM:[/] {s_low_bpm}-{s_peak_bpm}/m")
        console.print(f"    [dim]Profit:[/] [{pc}]{ps}{profit:.8f}[/]  [dim]Wagered:[/] {wagered:.8f}  [dim]Avg P/L:[/] [{avg_c}]{avg_s}{avg_pnl:.8f}[/]")
        console.print(f"    [dim]Balance:[/] {start_bal:.8f} -> {end_bal:.8f}  [dim]Peak:[/] [green]{hi_bal:.8f}[/]  [dim]Low:[/] [red]{lo_bal:.8f}[/]")
        console.print(f"    [dim]Streaks:[/] W+{mws}  L-{mls}  [dim]Best Win:[/] [green]+{hi_win:.8f}[/]  [dim]Worst Loss:[/] [red]-{big_loss:.8f}[/]")
        console.print()

    # totals -- same window as the listing; time span / bet time (WolfBet v2.34.0)
    span_first, span_last, span_sec, bet_sec = sessions_time_span(conn, where, params)
    totals = conn.execute(f"""
        SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
               COALESCE(SUM(losses),0), COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0),
               COALESCE(MAX(max_win_streak),0), COALESCE(MAX(max_loss_streak),0),
               COALESCE(MAX(profit),0), COALESCE(MIN(profit),0),
               COALESCE(MAX(total_bets),0), COALESCE(AVG(total_bets),0),
               COALESCE(MAX(highest_balance),0), COALESCE(MAX(highest_win),0),
               COALESCE(MAX(biggest_loss),0), COALESCE(AVG(bets_per_minute),0),
               COALESCE(MAX(peak_bps),0), COALESCE(MAX(peak_bpm),0),
               COALESCE(AVG(bets_per_second),0),
               COALESCE(MAX(wins),0), COALESCE(AVG(wins),0),
               COALESCE(MAX(losses),0), COALESCE(AVG(losses),0)
        FROM sessions{where}
    """, tuple(params)).fetchone()
    conn.close()

    sessions, tot_bets, tot_wins, tot_losses, tot_profit, tot_wagered, \
        best_ws, best_ls, best_profit, worst_profit, max_bets, avg_bets, \
        all_hi_bal, all_hi_win, all_big_loss, avg_bpm, \
        all_peak_bps, all_peak_bpm, avg_bps, \
        max_wins_s, avg_wins_s, max_losses_s, avg_losses_s = totals
    pc = "green" if tot_profit >= 0 else "red"
    ps = "+" if tot_profit >= 0 else ""
    wr = f"{tot_wins/tot_bets*100:.1f}%" if tot_bets > 0 else "--"
    bp_c = "green" if best_profit >= 0 else "red"
    wp_c = "green" if worst_profit >= 0 else "red"
    avg_pnl = tot_profit / tot_bets if tot_bets > 0 else 0
    avg_pc = "green" if avg_pnl >= 0 else "red"
    avg_ps = "+" if avg_pnl >= 0 else ""

    console.print(Rule(f"[bold cyan]{'Range' if (since or until or lane) else 'All-Time'} Totals[/]{window_label}"))
    rng = (f"  [dim]({span_first.isoformat(' ', 'seconds')} -> "
           f"{span_last.isoformat(' ', 'seconds')})[/]") if span_first else ""
    console.print(f"  [dim]Sessions:[/]      {sessions}{rng}")
    console.print(f"  [dim]Time Span:[/]     {fmt_duration(span_sec)}   "
                  f"[dim]Bet Time:[/] {fmt_duration(bet_sec)}")
    console.print(f"  [dim]Total Bets:[/]    {tot_bets}  (W {tot_wins} / L {tot_losses}  WR {wr})")
    console.print(f"  [dim]Wagered:[/]       {tot_wagered:.8f}")
    console.print(f"  [dim]Profit:[/]        [{pc}]{ps}{tot_profit:.8f}[/]")
    console.print(f"  [dim]Avg P/L:[/]       [{avg_pc}]{avg_ps}{avg_pnl:.8f}[/] per bet")
    console.print(f"  [dim]Best Session:[/]  [{bp_c}]+{best_profit:.8f}[/]")
    console.print(f"  [dim]Worst Session:[/] [{wp_c}]{worst_profit:.8f}[/]")
    console.print(f"  [dim]Peak Balance:[/]  {all_hi_bal:.8f}")
    console.print(f"  [dim]Best Win:[/]      [green]+{all_hi_win:.8f}[/]")
    console.print(f"  [dim]Worst Loss:[/]    [red]-{all_big_loss:.8f}[/]")
    console.print(f"  [dim]Best Streaks:[/]  W+{best_ws}  L-{best_ls}")
    console.print(f"  [dim]Speed:[/]         {avg_bps:.1f} avg/s  {avg_bpm:.0f} avg/m  [dim]Peak:[/] {int(all_peak_bps)}/s  {int(all_peak_bpm)}/m")
    console.print(f"  [dim]Bets/Session:[/]  Max {max_bets}  Avg {avg_bets:.0f}")
    console.print(f"  [dim]Wins/Session:[/]  Max {max_wins_s}  Avg {avg_wins_s:.1f}")
    console.print(f"  [dim]Losses/Session:[/] Max {max_losses_s}  Avg {avg_losses_s:.1f}")
    console.print()


# ===========================================================
#  ANALYTICS (WolfBet v2.23.0 / v2.31.0 / v2.34.0 / v2.35.0)
# ===========================================================
_ANALYTICS_COLS = ("sessions", "bets", "wins", "losses", "profit", "wagered")


def _analytics_lifetime(where: str = "", params=()) -> dict:
    """Top-line aggregates over the (optionally windowed) sessions."""
    conn = _db_conn()
    try:
        row = conn.execute(f"""
            SELECT COUNT(*)                     AS sessions_total,
                   COALESCE(SUM(total_bets), 0) AS bets_total,
                   COALESCE(SUM(wins), 0)       AS wins_total,
                   COALESCE(SUM(losses), 0)     AS losses_total,
                   COALESCE(SUM(wagered), 0)    AS wagered_total,
                   COALESCE(SUM(profit), 0)     AS profit_total,
                   COALESCE(MAX(profit), 0)     AS best_profit,
                   COALESCE(MIN(profit), 0)     AS worst_profit,
                   SUM(CASE WHEN profit >= 0 THEN 1 ELSE 0 END) AS sessions_in_profit,
                   SUM(CASE WHEN profit <  0 THEN 1 ELSE 0 END) AS sessions_in_loss
            FROM sessions{where}
        """, tuple(params)).fetchone()
    finally:
        conn.close()
    keys = ("sessions_total", "bets_total", "wins_total", "losses_total",
            "wagered_total", "profit_total", "best_profit", "worst_profit",
            "sessions_in_profit", "sessions_in_loss")
    d = dict(zip(keys, row))
    d["win_rate_pct"] = (d["wins_total"] / d["bets_total"] * 100) if d["bets_total"] > 0 else 0.0
    d["sessions_in_profit"] = d["sessions_in_profit"] or 0
    d["sessions_in_loss"] = d["sessions_in_loss"] or 0
    return d


def _analytics_group(key_sql: str, key_name: str, order_sql: str,
                     where: str = "", params=()) -> list:
    """Sessions aggregated by one key expression (game / strategy / lane)."""
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT {key_sql},
                   COUNT(*),
                   COALESCE(SUM(total_bets), 0),
                   COALESCE(SUM(wins), 0),
                   COALESCE(SUM(losses), 0),
                   COALESCE(SUM(profit), 0),
                   COALESCE(SUM(wagered), 0)
            FROM sessions{where}
            GROUP BY 1
            ORDER BY {order_sql}
        """, tuple(params)).fetchall()
    finally:
        conn.close()
    return [dict(zip((key_name,) + _ANALYTICS_COLS, r)) for r in rows]


def _analytics_by_game(where: str = "", params=()) -> list:
    rows = _analytics_group("COALESCE(game, 'limbo')", "game", "6 DESC", where, params)
    for r in rows:
        r["game"] = GAMES.get(r["game"], {}).get("label", r["game"])
    return rows


def _analytics_by_strategy(where: str = "", params=()) -> list:
    return _analytics_group("COALESCE(strategy, '?')", "strategy", "6 DESC", where, params)


def _analytics_by_lane(where: str = "", params=()) -> list:
    """v1.11.0 -- sessions per parallel lane (rows before lanes count as lane 1)."""
    rows = _analytics_group("COALESCE(lane, 1)", "lane", "1", where, params)
    for r in rows:
        r["lane"] = f"Lane {r['lane']}"
    return rows


def _analytics_top_sessions(order: str = "profit_desc", limit: int = 5,
                            where: str = "", params=()) -> list:
    """Top-N sessions by a whitelisted order key."""
    order_sql = {
        "profit_desc": "profit DESC",
        "profit_asc":  "profit ASC",
        "bets_desc":   "total_bets DESC",
    }.get(order, "profit DESC")
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT id, COALESCE(game, 'limbo'), COALESCE(strategy, '?'),
                   COALESCE(total_bets, 0), COALESCE(wins, 0), COALESCE(losses, 0),
                   COALESCE(profit, 0), COALESCE(wagered, 0), started_at
            FROM sessions{where}
            ORDER BY {order_sql}
            LIMIT ?
        """, (*params, limit)).fetchall()
    finally:
        conn.close()
    out = [dict(zip(("id", "game", "strategy", "bets", "wins", "losses",
                     "profit", "wagered", "started_at"), r)) for r in rows]
    for r in out:
        r["game"] = GAMES.get(r["game"], {}).get("label", r["game"])
    return out


def _print_analytics_table(title: str, rows: list, key_attr: str, key_label: str):
    if not rows:
        return
    t = Table(title=title, box=box.SIMPLE, expand=True)
    t.add_column(key_label, style="cyan")
    t.add_column("Sessions", justify="right")
    t.add_column("Bets",     justify="right")
    t.add_column("Win rate", justify="right")
    t.add_column("P/L",      justify="right")
    t.add_column("Wagered",  justify="right")
    for r in rows:
        wr = f"{r['wins']/r['bets']*100:.1f}%" if r['bets'] > 0 else "--"
        pc = 'green' if r['profit'] >= 0 else 'red'
        ps = '+' if r['profit'] >= 0 else ''
        t.add_row(str(r[key_attr]), f"{r['sessions']:,}", f"{r['bets']:,}",
                  wr, f"[{pc}]{ps}{r['profit']:.8f}[/]", f"{r['wagered']:.8f}")
    console.print(t)


def _print_top_sessions_table(title: str, rows: list):
    if not rows:
        return
    t = Table(title=title, box=box.SIMPLE, expand=True)
    t.add_column("ID",       justify="right")
    t.add_column("Game",     style="cyan")
    t.add_column("Strategy", style="magenta")
    t.add_column("Bets",     justify="right")
    t.add_column("Win rate", justify="right")
    t.add_column("P/L",      justify="right")
    t.add_column("Wagered",  justify="right")
    t.add_column("Started",  style="dim")
    for r in rows:
        wr = f"{r['wins']/r['bets']*100:.1f}%" if r['bets'] > 0 else "--"
        pc = 'green' if r['profit'] >= 0 else 'red'
        ps = '+' if r['profit'] >= 0 else ''
        started_short = (r['started_at'] or "").replace("T", " ")[:19]
        t.add_row(str(r['id']), str(r['game']), str(r['strategy']), f"{r['bets']:,}",
                  wr, f"[{pc}]{ps}{r['profit']:.8f}[/]", f"{r['wagered']:.8f}",
                  started_short)
    console.print(t)


def cmd_analytics(since: str = None, until: str = None, lane: int = None):
    """Aggregates over all saved sessions, optionally windowed / per lane."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found -- run a session first.[/]")
        return
    init_db()  # sessions.lane on DBs the bot hasn't opened yet
    try:
        where, wparams, wlabel = stats_date_window(since, until, lane)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return
    lifetime = _analytics_lifetime(where, wparams)
    if lifetime["sessions_total"] == 0:
        console.print("[yellow]No sessions in the DB yet -- run a session first.[/]")
        return

    console.print(Rule(
        f"[bold cyan]{'Range' if (since or until or lane) else 'Lifetime'} analytics[/] -- "
        f"{lifetime['sessions_total']:,} sessions, "
        f"{lifetime['bets_total']:,} bets, {lifetime['wagered_total']:.8f} wagered"
        f"{wlabel}"))

    p = lifetime['profit_total']
    pc = 'green' if p >= 0 else 'red'
    ps = '+' if p >= 0 else ''
    grid = Table.grid(padding=(0, 2))
    grid.add_row("Total sessions:", f"{lifetime['sessions_total']:,}")
    conn = _db_conn()
    try:
        a_first, a_last, a_span, a_bet = sessions_time_span(conn, where, wparams)
    finally:
        conn.close()
    if a_first:
        grid.add_row("Time range:", f"{a_first.isoformat(' ', 'seconds')} -> "
                                    f"{a_last.isoformat(' ', 'seconds')}")
    grid.add_row("Time span:",          fmt_duration(a_span))
    grid.add_row("Bet time:",           fmt_duration(a_bet))
    grid.add_row("Sessions in profit:", f"{lifetime['sessions_in_profit']:,}")
    grid.add_row("Sessions in loss:",   f"{lifetime['sessions_in_loss']:,}")
    grid.add_row("Total bets:", f"{lifetime['bets_total']:,}   W: {lifetime['wins_total']:,}   "
                                f"L: {lifetime['losses_total']:,}")
    grid.add_row("Win rate:",           f"{lifetime['win_rate_pct']:.1f}%")
    grid.add_row("Total wagered:",      f"{lifetime['wagered_total']:.8f}")
    grid.add_row("Total P/L:",          f"[{pc}]{ps}{p:.8f}[/]")
    grid.add_row("Best session:",       f"+{lifetime['best_profit']:.8f}")
    grid.add_row("Worst session:",      f"{lifetime['worst_profit']:.8f}")
    console.print(Panel(grid, title="[bold]Overall[/]", border_style=pc))

    _print_analytics_table("[bold]By game[/]", _analytics_by_game(where, wparams), "game", "Game")
    _print_analytics_table("[bold]By strategy[/]", _analytics_by_strategy(where, wparams),
                           "strategy", "Strategy")
    by_lane = _analytics_by_lane(where, wparams)
    if len(by_lane) > 1:
        _print_analytics_table("[bold]By lane[/]", by_lane, "lane", "Lane")

    _print_top_sessions_table("[bold]Top 5 best sessions[/]",
                              _analytics_top_sessions("profit_desc", 5, where, wparams))
    _print_top_sessions_table("[bold]Top 5 worst sessions[/]",
                              _analytics_top_sessions("profit_asc", 5, where, wparams))

    console.print("\n[dim]  Use --stats for recent sessions in detail, "
                  "or --session-bets ID to drill into one.[/]\n")

# ===========================================================
#  CONFIG CLI  (WolfBet v2.25.0 / v2.28.3: --config-show, --config-set)
# ===========================================================
_CONFIG_SHOW_FIELDS = [
    ("currency",              "Betting currency"),
    ("game",                  "Game (limbo / dice)"),
    ("multiplier_target",     "Payout multiplier"),
    ("dice_condition",        "Dice direction (above / below)"),
    ("dice_target",           "Dice roll target"),
    ("strategy",              "Strategy name"),
    ("strategy_key",          "Strategy key (1-7)"),
    ("base_bet",              "Base bet amount"),
    ("loss_mult",             "Loss multiplier (Martingale)"),
    ("win_mult",              "Win multiplier (Paroli / Anti-M)"),
    ("delay_martin_threshold","Delay Martingale N-loss threshold"),
    ("bet_delay",             "Seconds between bets (0 = full speed)"),
    ("max_profit",            "Stop when profit >= this"),
    ("max_loss",              "Stop when loss >= this"),
    ("max_bets",              "Stop after this many bets"),
    ("max_wins",              "Stop after this many wins"),
    ("stop_on_balance",       "Stop when balance floor reached"),
    ("profit_increment",      "Bump base_bet by this at every profit milestone"),
    ("profit_threshold",      "Profit interval between increments"),
    ("profit_increment_scope", "session = each session's profit - run = total since run start (--run-reset)"),
    ("recurring",             "Auto-restart cycle on non-loss stop"),
    ("recurring_delay",       "Seconds to pause between recurring cycles (0 = back-to-back)"),
    ("streak_reset_losses",   "Restart session on first win after N losses in a row (profit >= 0)"),
    ("streak_reset_delay",    "Seconds to pause before the streak-reset restart"),
    ("basebet_pct",           "Base bet as % of balance (0 = off)"),
    ("streak_delay_loss",     "Extra pause after N losses in a row  (N:sec)"),
    ("streak_delay_win",      "Extra pause after N wins in a row  (N:sec)"),
    ("streak_delay_bets",     "Extra pause every N bets  (N:sec)"),
    ("streakbet_loss",        "Bet factor after N losses  (N:factor)"),
    ("milestone_bets",        "Milestone every N bets (0 = off)"),
    ("milestone_wins",        "Milestone every N wins (0 = off)"),
    ("milestone_losses",      "Milestone every N losses (0 = off)"),
    ("milestone_profit",      "Milestone every X profit crossed (0 = off)"),
    ("proxy",                 "HTTP/HTTPS/SOCKS5 proxy URL (credentials masked)"),
    ("purge_days",            "Bet-row retention window in days (1-30)"),
    ("db_write_mode",         "Bet DB writer: async (default, batched) or sync (per-bet commit)"),
    ("custom_rules_text",     "Rule-Based custom rules"),
]
_SECRET_FIELDS = ("access_token", "lockdown_token", "cookie")


def _fmt_config_value(field: str, val) -> str:
    """Render one value for --config-show. Proxy and secrets are always masked."""
    if field == "proxy":
        return f"[cyan]{mask_proxy(val)}[/]" if val else "[dim]direct[/]"
    if field in _SECRET_FIELDS:
        return f"[dim]{_mask_key(val)}[/]" if val else "[dim](not set)[/]"
    if val is None or val == "":
        return "[dim]off[/]"
    if isinstance(val, bool):
        return "[green]on[/]" if val else "[dim]off[/]"
    if isinstance(val, tuple) and len(val) == 2:
        n, x = val
        return "[dim]off[/]" if n == 0 else f"{n}:{x}"
    if isinstance(val, float):
        if field in ("base_bet", "max_profit", "max_loss", "stop_on_balance",
                     "profit_increment", "profit_threshold", "milestone_profit"):
            return f"{val:.8f}"
        return f"{val:.4f}".rstrip("0").rstrip(".") or "0"
    if isinstance(val, str) and len(val) > 60:
        return val[:57] + "..."
    return str(val)


def cmd_config_show():
    """Print the saved config as a table. No wizard, no side effects."""
    if not load_config():
        console.print("[yellow]No config saved yet -- run the wizard first.[/]")
        console.print("[dim]  Try: python3 stake.py --setup-only[/]")
        sys.exit(1)
    tbl = Table(title=f"Stake AutoBot v{VERSION} -- Current Config",
                title_style="bold cyan", show_lines=False)
    tbl.add_column("Field",       style="bold yellow", no_wrap=True)
    tbl.add_column("Value",       style="cyan")
    tbl.add_column("Description", style="dim")
    for field, desc in [("access_token", "x-access-token (wizard only)"),
                        ("lockdown_token", "x-lockdown-token (wizard only)"),
                        ("cookie", "Browser cookie (wizard only)")]:
        tbl.add_row(field, _fmt_config_value(field, getattr(state, field)), desc)
    live = _monitor_read_state()
    if not (live and _monitor_get_pid(live)):
        live = None
    for field, desc in _CONFIG_SHOW_FIELDS:
        val = getattr(state, field, None)
        shown = _fmt_config_value(field, val)
        if field == "base_bet" and live and live.get("base_bet") is not None and val is not None:
            live_base = float(live["base_bet"])
            if abs(live_base - float(val)) > 1e-12:
                shown = (f"{shown}  [dim](live {live_base:.8f}, "
                         f"+{int(live.get('run_bumps') or 0)} bumps)[/]")
        tbl.add_row(field, shown, desc)
    console.print(tbl)
    try:
        st = os.stat(CONFIG_PATH)
        console.print(f"\n[dim]Config file:[/] [cyan]{CONFIG_PATH}[/]  "
                      f"[dim]mode={oct(st.st_mode & 0o777)}  size={st.st_size}B[/]")
    except OSError:
        pass
    # v1.13.0 (wolfbet v2.35.0 / v2.36.0) -- lane count, split mode, overrides.
    raw = _read_config_raw() or {}
    n_lanes, mode = lanes_count(raw), lane_split(raw)
    console.print(f"\n[dim]Lanes:[/] [bold]{n_lanes}[/]  [dim]split:[/] [bold]{mode}[/]"
                  + (f"  [dim]({LANE_SPLIT_HELP[mode]})[/]" if n_lanes > 1 else ""))
    for lane, entry in sorted(lane_registry(raw).items()):
        ov = lane_overrides(raw, lane, _LANE_FORBIDDEN_KEYS)
        named = entry["name"] != default_lane_name(lane)
        if not (ov or named or not entry["enabled"]):
            continue
        label = f"  [magenta]lane{lane}[/]" + (f" [bold]{escape(entry['name'])}[/]" if named else "")
        if not entry["enabled"]:
            label += " [dim](off)[/]"
        parts = ", ".join(
            f"{k}={_fmt_config_value(k, parse_streak_delay(v) if k in _STREAK_TUPLE_KEYS else v)}"
            for k, v in sorted(ov.items()))
        console.print(label + (f" overrides: {parts}" if parts else ""))
    console.print("[dim]Tweak with:[/] [cyan]python3 stake.py --config-set KEY=VALUE ...[/]")


def _parse_config_set_pairs(pairs: list) -> dict:
    """['maxprofit=0.005', 'base_bet=0.0001'] -> {'max_profit': '0.005', ...}. Raises ValueError."""
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Malformed pair (expected KEY=VALUE): {pair!r}")
        key, val = pair.split("=", 1)
        key = key.strip().lower()
        canonical = _TWEAK_ALIAS.get(key, key)
        if canonical not in _TWEAK_PARAMS:
            raise ValueError(f"Unknown config field: {key!r}. Valid: {', '.join(sorted(_TWEAK_PARAMS))}")
        out[canonical] = val.strip()
    return out


def cmd_config_set(pairs: list):
    """Apply KEY=VALUE to the saved config, then restart the lanes whose account changed."""
    before = _lane_accounts(_read_config_raw() or {})
    _config_set_write(pairs)
    _restart_changed_lanes(before, _lane_accounts(_read_config_raw() or {}))


def _lane_accounts(raw: dict) -> dict:
    """{lane: (key_ref, currency, proxy, proxy_ref)} for the enabled lanes: what a restart must
    pick up. A proxy is part of it: the lane's Cloudflare session belongs to its IP."""
    reg = lane_registry(raw)
    out = {}
    for lane in sorted(reg):
        if reg[lane]["enabled"]:
            out[lane] = lane_account(raw, lane, _LANE_FORBIDDEN_KEYS, VAULT_KEY_ID)
    return out


def _lane_running(lane: int) -> bool:
    try:
        with open(lane_path(_BASE_PID_PATH, lane)) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def _restart_lane(lane: int) -> bool:
    """Restart one lane's unit (lane 1 is the main service). False when systemd can't do it."""
    import shutil
    import subprocess
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    unit = "stake" if lane <= 1 else f"stake-lane@{lane}"
    try:
        done = subprocess.run([systemctl, "--user", "restart", unit],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _restart_changed_lanes(before: dict, after: dict) -> None:
    """Restart, one by one, the running lanes whose key, currency or proxy changed."""
    for lane, account in after.items():
        if lane not in before or before[lane] == account or not _lane_running(lane):
            continue
        if _restart_lane(lane):
            console.print(f"Lane {lane} restarted: its key, currency or proxy changed.")
        else:
            console.print(f"[yellow]Restart lane {lane} to apply: [cyan]stakectl restart {lane}[/][/]")


def _config_set_write(pairs: list):
    """Apply KEY=VALUE changes to the saved config, save, and SIGHUP a running daemon."""
    for p in pairs:
        key = p.split("=", 1)[0].strip().lower().split(".", 1)[-1]
        if key in _SECRET_FIELDS or key in ("token", "lockdown"):
            console.print(f"[red]Refusing to write {key} via --config-set.[/]")
            console.print("[dim]  Credentials are set in the wizard (python3 stake.py --setup-only), "
                          "never in shell history / process args.[/]")
            sys.exit(2)
    # lanes=N, lanes_max=N, lane_split=MODE, laneK.name=NAME and laneK.key=value write the
    # lane registry directly.
    try:
        pairs, per_lane, block = split_registry_pairs(pairs)
    except ValueError as e:
        console.print(f"[red]Invalid --config-set argument:[/] {e}")
        sys.exit(2)
    if per_lane or block:
        rc = _cmd_config_set_lanes(per_lane, block)
        if rc:
            sys.exit(rc)
        if not pairs:
            return
    try:
        parsed = _parse_config_set_pairs(pairs)
    except ValueError as e:
        console.print(f"[red]Invalid --config-set argument:[/] {e}")
        sys.exit(2)
    if not load_config():
        console.print("[yellow]No config saved yet -- run the wizard first.[/]")
        console.print("[dim]  Try: python3 stake.py --setup-only[/]")
        sys.exit(1)

    old = {k: getattr(state, k, None) for k in parsed}
    try:
        mutation_msgs = _apply_mutation(parsed) or []
    except ValueError as e:
        console.print(f"[red]Mutation failed:[/] {e}")
        sys.exit(2)

    save_config()
    console.print(Rule("[bold cyan]Config updated[/]"))
    for k in parsed:
        console.print(f"  [yellow]{k}[/]: [dim]{_fmt_config_value(k, old[k])}[/]"
                      f"  ->  [green]{_fmt_config_value(k, getattr(state, k, None))}[/]")
    for m in mutation_msgs:
        if isinstance(m, str) and m.startswith("⚠"):
            console.print(f"  [yellow]{m}[/]")
    console.print(f"\n[dim]Saved to[/] [cyan]{CONFIG_PATH}[/]")

    unsafe = _restart_only(parsed)
    if unsafe:
        console.print(f"[yellow]Restart every lane to apply:[/] {', '.join(unsafe)} "
                      "[dim](stakectl restart).[/]")
    try:
        if not os.path.exists(PID_PATH):
            console.print("[dim]Daemon not running; changes apply on next start.[/]")
            return
        with open(PID_PATH) as f:
            daemon_pid = int(f.read().strip())
        if not hasattr(signal, "SIGHUP"):
            console.print("[dim]SIGHUP not supported on this platform; restart the daemon to apply.[/]")
            return
        os.kill(daemon_pid, signal.SIGHUP)
        console.print("[dim]Daemon reloaded live config.[/]")
    except (OSError, ValueError):
        console.print("[dim]Daemon PID stale; changes apply on next start.[/]")
    finally:
        # The base config is shared: reload the other registry lanes too.
        n_lanes = _signal_lane_daemons([k for k in lane_ids(_read_config_raw() or {}) if k > 1])
        if n_lanes:
            console.print(f"[dim]{n_lanes} more lane(s) reloaded live config.[/]")


def _read_config_raw() -> Optional[dict]:
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_config_raw(data: dict) -> None:
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def _lane_override_changes(lane_pairs: list):
    """v1.13.0 (wolfbet v2.35.0) -- validate laneK.* pairs through the normal
    mutation path. Returns (sets, removals): sets = {key: config-file value},
    including derived fields (dice_target after multiplier); removals = keys
    whose override goes away (value default / unset / inherit)."""
    removals, to_set = set(), []
    for pair in lane_pairs:
        k, v = pair.split("=", 1)
        canon = _TWEAK_ALIAS.get(k.strip().lower(), k.strip().lower())
        check_lane_keys([canon], _LANE_FORBIDDEN_KEYS)
        if canon == "proxy" and "@" in v:
            raise ValueError("a proxy with a password goes in the vault: add it with "
                             "stakectl key add ID --kind proxy and set proxy_ref")
        if v.strip().lower() in LANE_UNSET_VALUES:
            removals.add(canon)
        else:
            to_set.append(pair)
    parsed = _parse_config_set_pairs(to_set) if to_set else {}
    check_lane_keys(parsed, _LANE_FORBIDDEN_KEYS)
    if not load_config():
        raise ValueError("No config saved yet -- run the wizard first.")
    before = {k: _config_value(k) for k in _CONFIG_KEYS}
    _apply_mutation(parsed)
    changes = {k: _config_value(k) for k in _CONFIG_KEYS if _config_value(k) != before[k]}
    for k in parsed:   # same value as base still pins the override
        changes.setdefault(k, _config_value(k))
    check_lane_keys(changes, _LANE_FORBIDDEN_KEYS)
    return changes, removals


def _signal_lane_daemons(lanes=None) -> int:
    """SIGHUP every running lane daemon (or just `lanes`). Returns count signalled."""
    if not hasattr(signal, "SIGHUP"):
        return 0
    n = 0
    for k in (lanes if lanes is not None else lane_ids(_read_config_raw() or {})):
        try:
            with open(lane_path(_BASE_PID_PATH, k)) as f:
                os.kill(int(f.read().strip()), signal.SIGHUP)
            n += 1
        except (OSError, ValueError):
            continue
    return n


def _backup_config_once(suffix: str, reason: str) -> None:
    """Copy the config file to CONFIG_PATH + suffix (0600) unless that copy already exists."""
    backup = CONFIG_PATH + suffix
    if os.path.exists(backup) or not os.path.exists(CONFIG_PATH):
        return
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, open(CONFIG_PATH, "rb") as src:
            out.write(src.read())
    except OSError as e:
        logger.warning("could not back up the config before %s: %s", reason, e)


def _save_lane_registry(raw: dict, reg: dict, next_id: int, split=None, limit=None) -> None:
    """Write the lane registry; a config still in the numbered-lanes shape is copied once to
    `.pre-registry` first, so older code can be restored."""
    old = raw.get("lanes")
    if isinstance(old, dict) and "items" not in old:
        _backup_config_once(".pre-registry", "the lane registry")
    raw["lanes"] = registry_block(raw, reg, next_id, split, limit)
    _write_config_raw(raw)


def _signal_share_change(before: dict, after: dict) -> None:
    """After the lane count changes, bring the remaining lanes their new share.

    With the bet split each running lane reloads, so its base bet / lanes follows at once.
    The budget share (balance / lanes) is fixed when a lane starts, so it waits for a restart.
    """
    if lanes_count(before) == lanes_count(after):
        return
    split = lane_split(after)
    if split in ("bet", "both"):
        enabled = [lane for lane, entry in lane_registry(after).items() if entry["enabled"]]
        n = _signal_lane_daemons(enabled)
        if n:
            console.print(f"[dim]{n} running lane(s) reloaded their base bet share.[/]")
    if split in ("budget", "both"):
        console.print("[dim]Budget shares update when each lane restarts: "
                      "[cyan]stakectl restart K[/].[/]")


def _cmd_config_set_lanes(per_lane: dict, block: dict) -> int:
    """Write lanes=N, lanes_max, lane_split, lane names and overrides to the registry; SIGHUP
    the lanes touched. Nothing is written when any of it is refused."""
    raw = _read_config_raw()
    if raw is None:
        console.print("[yellow]No config saved yet -- run the wizard first.[/]")
        return 1
    before = json.loads(json.dumps(raw))
    reg = lane_registry(raw)
    next_id = next_lane_id(raw, reg)
    count, split, limit = block.get("count"), block.get("split"), block.get("max")
    lines, restart_notes = [], {}
    effective_limit = limit if limit is not None else lanes_max(raw)
    if limit is not None:
        lines.append(f"  [yellow]lanes_max[/]: [dim]{lanes_max(raw)}[/]  ->  [green]{limit}[/]")
    if count is not None:
        if count > effective_limit:
            console.print(f"[red]Invalid --config-set argument:[/] lanes must be "
                          f"1-{effective_limit}, got {count}")
            console.print("[dim]  Raise the limit with: stakectl lanes --max N[/]")
            return 2
        lines.append(f"  [yellow]lanes[/]: [dim]{lanes_count(raw)}[/]  ->  [green]{count}[/]")
        try:
            next_id = set_enabled_count(reg, count, next_id)
        except ValueError as e:
            console.print(f"[red]Invalid --config-set argument:[/] {e}")
            return 2
    running = sum(1 for entry in reg.values() if entry["enabled"])
    if limit is not None and limit < running:
        console.print(f"[red]Invalid --config-set argument:[/] lanes_max can't be below the "
                      f"{running} lane(s) configured")
        return 2
    if split is not None:
        lines.append(f"  [yellow]lane_split[/]: [dim]{lane_split(raw)}[/]  ->  [green]{split}[/]  "
                     f"[dim]({LANE_SPLIT_HELP[split]})[/]")
    for lane, lane_pairs in sorted(per_lane.items()):
        names = [p.split("=", 1)[1] for p in lane_pairs if p.startswith("name=")]
        lane_pairs = [p for p in lane_pairs if not p.startswith("name=")]
        entry = reg.setdefault(lane, {"name": default_lane_name(lane), "enabled": False,
                                      "overrides": {}})
        for name in names[-1:]:
            lines.append(f"  [yellow]lane{lane}.name[/]: [dim]{escape(entry['name'])}[/]  ->  "
                         f"[green]{escape(name)}[/]")
            entry["name"] = name
        if not lane_pairs:
            continue
        try:
            sets, removals = _lane_override_changes(lane_pairs)
        except ValueError as e:
            console.print(f"[red]Invalid lane{lane} override:[/] {e}")
            return 2
        ov = entry["overrides"]
        restart_notes[lane] = _restart_only(list(sets) + list(removals))
        for k in sorted(removals):
            ov.pop(k, None)
            lines.append(f"  [yellow]lane{lane}.{k}[/]: [dim]override removed (uses base)[/]")
        for k, v in sets.items():
            ov[k] = v
            shown = parse_streak_delay(v) if k in _STREAK_TUPLE_KEYS else v
            lines.append(f"  [yellow]lane{lane}.{k}[/]  ->  [green]{_fmt_config_value(k, shown)}[/]")
    _save_lane_registry(raw, reg, max(next_id, max(reg) + 1), split, limit)
    console.print(Rule("[bold cyan]Lane config updated[/]"))
    for line in lines:
        console.print(line)
    console.print(f"\n[dim]Saved to[/] [cyan]{CONFIG_PATH}[/]")
    for lane, keys in sorted(restart_notes.items()):
        if keys:
            console.print(f"[yellow]Restart lane {lane} to apply:[/] {', '.join(keys)} "
                          f"[dim](stakectl restart {lane}).[/]")
    if per_lane:
        n = _signal_lane_daemons(sorted(per_lane))
        if n:
            console.print(f"[dim]{n} running lane(s) reloaded live config "
                          "(restart-only fields apply on the next start).[/]")
    if count is not None:
        _signal_share_change(before, raw)
    elif split is not None:
        console.print("[dim]lane_split applies on the next lane start -- run "
                      "[cyan]stakectl restart[/].[/]")
    return 0


def cmd_lane_add(name: str, src=None) -> int:
    """Add one enabled lane named `name`, copying lane `src`'s overrides (lane 1: none)."""
    src = 1 if src is None else int(src)
    name = (name or "").strip()
    if not LANE_NAME_RE.match(name):
        console.print(f"[red]{LANE_NAME_HELP}[/]")
        return 2
    raw = _read_config_raw()
    if raw is None:
        console.print("[yellow]No config saved yet -- run the wizard first.[/]")
        return 1
    before = json.loads(json.dumps(raw))
    reg = lane_registry(raw)
    if src not in reg:
        console.print(f"[red]No lane {src}.[/]")
        return 2
    limit, running = lanes_max(raw), lanes_count(raw)
    if running >= limit:
        console.print(f"[red]This bot already runs {running} lane(s), the limit.[/]")
        console.print("[dim]  Raise it with: stakectl lanes --max N[/]")
        return 2
    lane = next_lane_id(raw, reg)
    if lane > LANE_ID_MAX:
        console.print(f"[red]Lane ids run out at {LANE_ID_MAX}: removed ids are never reused.[/]")
        return 2
    overrides = dict(reg[src]["overrides"]) if src > 1 else {}
    reg[lane] = {"name": name, "enabled": True, "overrides": overrides}
    _save_lane_registry(raw, reg, lane + 1)
    copied = f" with lane {src}'s overrides" if overrides else ""
    console.print(f"Lane {lane} ({escape(name)}) added{copied}.")
    _signal_share_change(before, raw)
    return 0


def cmd_lane_drain(lane: int, action: str) -> int:
    """Ask lane `lane` to restart or be removed when its current session ends, or cancel that."""
    if action == "remove" and lane == 1:
        console.print("[red]Lane 1 holds the base config and can't be removed.[/]")
        return 2
    raw = _read_config_raw()
    if raw is None:
        console.print("[yellow]No config saved yet -- run the wizard first.[/]")
        return 1
    if lane not in lane_registry(raw):
        console.print(f"[red]No lane {lane}.[/]")
        return 2
    request_drain(DRAIN_PATH, lane, action)
    if action == "cancel":
        console.print(f"Lane {lane} no longer waits for its session end.")
        return 0
    done = "restarts" if action == "restart" else "is removed"
    console.print(f"Lane {lane} {done} when its session ends. It keeps betting until then.")
    return 0


def _session_boundary(final: bool) -> bool:
    """Carry out a restart or removal the lane was asked to do at its session end.

    Read once per session close, never per bet. The request is deleted before it is acted
    on, so a lane that fails to come back never loops on it. A restart re-executes this
    process on the saved config and code; with `final` (recurring off) the lane stops as
    usual and the restart is dropped. Returns True when the lane must stop now.
    """
    try:
        action = take_drain(DRAIN_PATH, state.lane)
    except OSError as e:
        logger.warning("Lane %d: session-end request unreadable: %s", state.lane, e)
        return False
    if action == "remove":
        logger.warning("Lane %d: removed at the session boundary", state.lane)
        import subprocess
        with open(os.devnull, "wb") as null:
            subprocess.Popen([STAKECTL, "lane", "remove", str(state.lane)],
                             stdout=null, stderr=null, start_new_session=True)
        return True
    if action != "restart":
        return False
    if final:
        logger.info("Lane %d: restart request dropped, the lane stops at its session end",
                    state.lane)
        return False
    logger.warning("Lane %d: restarting at the session boundary", state.lane)
    _stop_bet_writer(timeout=5.0)
    _save_state_file()
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return True


def cmd_lane_remove(lane: int) -> int:
    """Drop lane `lane` from the registry; its id is never reused and its history stays."""
    if lane == 1:
        console.print("[red]Lane 1 holds the base config and can't be removed.[/]")
        return 2
    raw = _read_config_raw()
    if raw is None:
        console.print("[yellow]No config saved yet -- run the wizard first.[/]")
        return 1
    reg = lane_registry(raw)
    if lane not in reg:
        console.print(f"[red]No lane {lane}.[/]")
        return 2
    before = json.loads(json.dumps(raw))
    next_id = next_lane_id(raw, reg)
    name = reg.pop(lane)["name"]
    _save_lane_registry(raw, reg, next_id)
    console.print(f"Lane {lane} ({escape(name)}) removed.")
    _signal_share_change(before, raw)
    return 0


def _key_users(key_id: str, kind: str) -> list:
    """The enabled lanes that bet with vault entry `key_id`: as their account, or their proxy."""
    if kind == "telegram":
        return []
    at = 0 if kind == "account" else 3
    return [lane for lane, account in _lane_accounts(_read_config_raw() or {}).items()
            if account[at] == key_id]


def _read_secret(kind: str):
    """The secret from stdin, where it never shows in a process list: one line, or for an
    account three (access token, lockdown token, cookie)."""
    if kind != "account":
        secret = sys.stdin.readline().rstrip("\r\n")
        check_secret(secret, kind)
        return secret
    fields = {f: sys.stdin.readline().rstrip("\r\n") for f in ACCOUNT_FIELDS}
    check_account(fields, ACCOUNT_FIELDS)
    check_secret(fields["access_token"], "key")
    return fields


def cmd_key(action: str, key_id: Optional[str] = None, label: Optional[str] = None,
            kind: str = "account") -> int:
    """List, add, rotate or delete vault entries. No secret is ever printed or logged."""
    try:
        vault = Vault(VAULT_SERVICE)
        if action == "list":
            t = Table(title="Vault keys", title_style="bold cyan", box=box.SIMPLE)
            for col in ("Id", "Kind", "Label", "Ends", "Lanes"):
                t.add_column(col)
            for info in vault.keys():
                lanes = ", ".join(str(k) for k in _key_users(info.key_id, info.kind)) or "-"
                t.add_row(info.key_id, info.kind, escape(info.label), info.last4, lanes)
            console.print(t)
            return 0
        if not KEY_ID_RE.match(key_id or ""):
            raise ValueError("key id must be 1-32 lowercase letters, digits, _ or -")
        old = vault.info(key_id)
        if action == "put":
            if (kind == "telegram") != (key_id == TG_KEY_ID):
                raise ValueError(f"The Telegram bot token is stored as {TG_KEY_ID}, and only there.")
            if old is not None:
                raise ValueError(f"A key named {key_id} already exists.")
        elif old is None:
            raise ValueError(f"No key named {key_id}.")
        if action == "delete":
            users = _key_users(key_id, old.kind)
            if users:
                raise ValueError(f"This key is used by lane {', '.join(str(k) for k in users)}. "
                                 "Move the lanes to another key first.")
            vault.delete(key_id)
            logger.info("Vault key %s deleted", key_id)
            console.print(f"Key {key_id} deleted.")
            return 0
        if old is not None and kind != old.kind:
            raise ValueError(f"{key_id} holds a {old.kind}: rotate it with --kind {old.kind}.")
        secret = _read_secret(kind)
        label = label or (old.label if old else key_id)
        if kind == "account":
            info = vault.put_account(key_id, secret, label, primary="access_token")
        else:
            info = vault.put(key_id, secret, label, kind=kind)
    except ValueError as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 2
    except (VaultError, OSError) as e:
        logger.warning("Key vault unavailable: %s", e)
        console.print("[red]The key vault can't be used on this host.[/]")
        return 1
    done = "added" if action == "put" else "rotated"
    logger.info("Vault key %s %s", key_id, done)
    console.print(f"Key {key_id} {done} (ends {info.last4}).")
    if kind == "telegram":
        console.print("Restart the Telegram bot to use it: stakectl tg restart")
    elif action == "rotate":
        console.print("Lanes using it pick up the new one when they restart.")
    return 0


# ===========================================================
#  RETENTION  (WolfBet v2.26.0)
# ===========================================================
def cmd_cleanup(days=None):
    """Purge bet rows of sessions ended more than N days ago; session rows are kept."""
    load_config()
    n = int(days) if days is not None else state.purge_days
    n = max(1, min(30, n))
    if not os.path.exists(DB_PATH):
        console.print("[dim]No database found -- nothing to purge.[/]")
        return
    try:
        deleted = int(cleanup_old_bets(DB_PATH, bet_age_days=n) or 0)
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Cleanup failed{detail}.[/]")
        sys.exit(1)
    if deleted:
        console.print(f"[green]✓[/] Purged [bold]{deleted}[/] bet rows from sessions older than "
                      f"[bold]{n}[/] days. Session aggregates preserved.")
    else:
        console.print(f"[dim]Nothing to purge ({n}-day window; no old session bets found).[/]")


def _retention_worker():
    """Hourly purge of bet rows older than purge_days; exits when the bot stops."""
    while state.running or state.paused:
        for _ in range(3600):
            if not (state.running or state.paused):
                return
            time.sleep(1)
        try:
            deleted = cleanup_live_bets(DB_PATH, bet_age_days=state.purge_days)
            if deleted:
                logger.info("RETENTION purged %d bet rows (>%dd)", deleted, state.purge_days)
        except Exception as e:
            logger.warning("retention purge failed: %s", e)


# ===========================================================
#  BPS DIAGNOSTICS  (WolfBet v2.26.1: --benchmark, --diagnose)
# ===========================================================
def _bench_session():
    """A fresh HTTP client like the live one: one per benchmark lane (v1.13.0)."""
    if _HAS_CFFI:
        return cffi_requests.Session(impersonate="chrome", proxy=state.proxy or None)
    return requests.Session()


def _bench_lanes(n: int, lanes: int) -> dict:
    """`lanes` clients, each in its own thread with its own session, fire n
    read-only UserBalances calls at once. Returns rps, p50 and status counts."""
    lock = threading.Lock()
    samples, counts = [], {"ok": 0, "total": 0, "s429": 0, "s403": 0}

    def worker():
        sess = _bench_session()
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                r = sess.post(_gql_url(), headers=_headers(), json=_user_balances_payload(),
                              timeout=15)
                code = r.status_code
            except Exception:
                code = None
            ms = (time.perf_counter() - t0) * 1000
            with lock:
                counts["total"] += 1
                if code == 200:
                    counts["ok"] += 1
                    samples.append(ms)
                elif code == 429:
                    counts["s429"] += 1
                elif code == 403:
                    counts["s403"] += 1

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(lanes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = max(time.perf_counter() - t0, 1e-9)
    samples.sort()
    return {"rps": counts["ok"] / wall, "p50": percentile(samples, 0.50), **counts}


def cmd_benchmark_lanes(n: int, lanes: int):
    """v1.13.0 (wolfbet v2.35.0 `--benchmark --lanes M`) -- probe at 1..M
    concurrent clients and recommend a lane count. Read-only, no CF solver."""
    lanes = max(2, min(LANES_MAX, int(lanes)))
    console.print(f"[dim]Benchmarking {n} x GraphQL UserBalances per client at 1..{lanes} "
                  f"concurrent clients (proxy={mask_proxy(state.proxy)}) ...[/]")
    rows = {k: _bench_lanes(n, k) for k in range(1, lanes + 1)}
    tbl = Table(title="Concurrent lanes", title_style="bold cyan", box=box.SIMPLE)
    for col in ("lanes", "req/s", "x1 lane", "p50 ms", "ok", "429", "403"):
        tbl.add_column(col, justify="right")
    base = rows[1]["rps"]
    for k, r in rows.items():
        tbl.add_row(str(k), f"{r['rps']:.1f}", f"{r['rps'] / base:.2f}" if base else "-",
                    f"{r['p50']:.0f}", f"{r['ok']}/{r['total']}",
                    f"[red]{r['s429']}[/]" if r["s429"] else "0",
                    f"[yellow]{r['s403']}[/]" if r["s403"] else "0")
    console.print(tbl)
    if not base:
        console.print("[red]No successful requests. Check tokens, proxy and network.[/]")
        sys.exit(1)
    best = recommend_lanes(base, {k: r for k, r in rows.items() if k > 1})
    console.print(f"[bold green]Recommended lanes: {best}[/]  [dim](the highest count where "
                  "each step adds >= 30% req/s with no 429)[/]")
    if any(r["s403"] for r in rows.values()):
        console.print("[yellow]Some requests got 403 (Cloudflare). Start the bot once so it "
                      "solves CF and caches cookies, then re-run.[/]")


def cmd_benchmark(n=None, proxy=None, lanes=None):
    """N read-only GraphQL UserBalances calls through the live client: no bets, no DB.

    Uses the same CF-aware session, headers, cached CF cookies and proxy as
    the bet path. It does not run the CF solver chain: a 403 counts as a
    failed sample. With lanes > 1 it probes concurrent clients instead.
    """
    n = 20 if n is None else int(n)
    n = max(3, min(200, n))
    if not load_config():
        console.print("[red]No config saved yet -- run the wizard first.[/]")
        sys.exit(1)
    if proxy:                      # --proxy beats the saved one
        state.proxy = proxy
        _recreate_http()
    _cf_cache_load()
    if lanes is not None and int(lanes) > 1:
        cmd_benchmark_lanes(n, lanes)
        return
    console.print(f"[dim]Benchmarking {n} x GraphQL UserBalances at {_gql_url()} "
                  f"(proxy={mask_proxy(state.proxy)}) ...[/]")
    samples, statuses, failures = [], [], 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            r = _http.post(_gql_url(), headers=_headers(), json=_user_balances_payload(), timeout=15)
            samples.append((time.perf_counter() - t0) * 1000)
            statuses.append(r.status_code)
        except Exception:
            failures += 1
            statuses.append("ERR")
    if not samples:
        console.print("[red]All requests failed. Check tokens, proxy and network.[/]")
        sys.exit(1)
    samples.sort()
    p50 = percentile(samples, 0.50)
    p99 = percentile(samples, 0.99)
    mean = sum(samples) / len(samples)
    tbl = Table(title="API latency (ms)", title_style="bold cyan", show_lines=False)
    for col, style in [("min", "green"), ("p50", "cyan"), ("mean", "cyan"),
                       ("p99", "yellow"), ("max", "red"), ("ok", "dim")]:
        tbl.add_column(col, style=style, justify="right")
    ok_count = sum(1 for st in statuses if st == 200)
    tbl.add_row(f"{min(samples):.0f}", f"{p50:.0f}", f"{mean:.0f}",
                f"{p99:.0f}", f"{max(samples):.0f}", f"{ok_count}/{n}")
    console.print(tbl)
    max_bps = 1000.0 / p50 if p50 > 0 else 0.0
    console.print(f"\n[dim]Theoretical max BPS from p50:[/] [bold magenta]{max_bps:.1f}[/] bps")
    colour, msg = benchmark_verdict(p50=p50, p99=p99, statuses=statuses,
                                    sample_count=n, casino_name="Stake")
    console.print(f"[bold {colour}]{msg}[/]")
    blocked = sum(1 for st in statuses if st == 403)
    if blocked:
        console.print(f"[yellow]{blocked}/{n} got 403 (Cloudflare). Start the bot once so it "
                      "solves CF and caches cookies, then re-run.[/]")
    if failures:
        console.print(f"[dim]Note: {failures}/{n} network errors.[/]")


def cmd_diagnose():
    """One-shot bottleneck analysis of the running session, from the state file."""
    if not os.path.exists(STATE_PATH):
        console.print("[yellow]No running daemon session found.[/]\n"
                      "  Start one with [cyan]stakectl start[/] then re-run "
                      "[cyan]python3 stake.py --diagnose[/].")
        return
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Failed to read state file{detail}.[/]")
        return

    api_p50    = float(d.get("api_p50_ms") or 0)
    overhead   = float(d.get("overhead_ms") or 0)
    cycle_ms   = float(d.get("cycle_ms") or 0)
    cycle_p50  = float(d.get("cycle_p50_ms") or cycle_ms)
    cycle_p99  = float(d.get("cycle_p99_ms") or 0)
    cycle_n    = int(d.get("cycle_sample_count") or 0)
    recent_bps = float(d.get("bets_per_second_recent") or 0)
    bet_delay  = float(d.get("bet_delay") or 0)
    bps        = float(d.get("bets_per_second") or 0)
    tmax       = float(d.get("theoretical_max_bps") or 0)
    delay_ms   = bet_delay * 1000

    eff_ref = recent_bps if recent_bps > 0 else bps
    if tmax > 0:
        eff_raw = eff_ref / tmax * 100.0
        eff_display = ">100%" if eff_raw > 100.0 else f"{eff_raw:.0f}%"
    else:
        eff_display = "--"

    lines = [
        f"[dim]Game:[/] [cyan]{d.get('game', '?')}[/]  ·  [dim]Strategy:[/] [cyan]{d.get('strategy', '?')}[/]",
        "",
        (f"[bold]Actual:[/] [magenta]{bps:.1f}[/] BPS (session)  ·  "
         f"[bold]Recent:[/] [magenta]{recent_bps:.1f}[/] BPS  ·  "
         f"[bold]Theoretical:[/] [magenta]{tmax:.1f}[/] BPS  ·  "
         f"[bold]Efficiency:[/] [magenta]{eff_display}[/]"),
        (f"[dim]Cycle:[/] median [cyan]{cycle_p50:.0f}ms[/]  p99 [cyan]{cycle_p99:.0f}ms[/]  "
         f"[dim](n={cycle_n} samples)[/]"),
        (f"[dim]Cycle breakdown:[/] api_p50=[cyan]{api_p50:.0f}ms[/] "
         f"+ overhead=[cyan]{overhead:.0f}ms[/] + delay=[cyan]{delay_ms:.0f}ms[/] "
         f"[dim]≈[/] [magenta]{api_p50 + overhead + delay_ms:.0f}ms[/]/bet [dim](decomposition)[/]"),
        (f"[dim]Measured cycle:[/]  [magenta]{cycle_ms:.0f}ms[/]/bet "
         f"[dim](median of last {cycle_n} iterations)[/]"),
    ]

    db_mode = d.get("db_write_mode") or "sync"
    dbw = d.get("db_writer_stats") or {}
    dbw_fb = int(dbw.get("fallback_sync") or 0)
    dbw_hwm = int(dbw.get("queue_high_watermark") or 0)
    if db_mode == "async":
        lines.append(
            f"[dim]DB writer:[/] mode=[cyan]async[/]  queue=[cyan]{int(d.get('db_writer_queue') or 0)}/1000[/]  "
            f"batches=[cyan]{int(dbw.get('batches') or 0)}[/]  flushed=[cyan]{int(dbw.get('flushed') or 0)}[/]  "
            f"fallback_sync=[{'red' if dbw_fb else 'cyan'}]{dbw_fb}[/]  hwm=[cyan]{dbw_hwm}[/]")
    else:
        lines.append("[dim]DB writer:[/] mode=[yellow]sync[/] (per-bet commit)")

    blockers = [("Network / API latency", api_p50),
                ("CLI overhead",          overhead),
                ("User-configured delay", delay_ms)]
    top_name, top_val = max(blockers, key=lambda x: x[1])

    advice = []
    if top_name == "Network / API latency" and api_p50 >= 100:
        advice.append("• Run [cyan]python3 stake.py --benchmark 20[/] to isolate the network path.")
        advice.append("• Consider a proxy in a region closer to Stake, or a faster server.")
    elif top_name == "User-configured delay" and delay_ms > 10:
        advice.append(f"• You have bet_delay={bet_delay:.3f}s. Drop it with "
                      "[cyan]python3 stake.py --config-set delay=0[/] for max throughput.")
    elif top_name == "CLI overhead" and overhead > 30:
        if db_mode == "sync":
            advice.append("• CLI overhead high AND db_write_mode=sync -- flip to async: "
                          "[cyan]python3 stake.py --config-set dbmode=async[/] then restart.")
        elif dbw_fb == 0 and dbw_hwm < 100:
            advice.append("• CLI overhead is high on this host. The DB writer is healthy, so it is "
                          "likely shared-tenancy CPU/disk jitter, not a fixable code issue.")
        else:
            advice.append("• CLI overhead is high. Check disk I/O (DB writes) and CPU load.")
    if dbw_fb > 0:
        advice.append(f"• DB writer queue overflowed {dbw_fb} times (check disk perf).")
    if not advice:
        advice.append("• Nothing obvious. Session is running near its natural ceiling.")

    lines += ["", f"[bold yellow]Top blocker:[/] {top_name} ([magenta]{top_val:.0f}ms[/])",
              "", "[bold]Advice:[/]", *advice]
    console.print(Panel("\n".join(lines), title="Diagnostic bottleneck analysis", border_style="cyan"))


# ===========================================================
#  MAIN
# ===========================================================
def _load_and_connect(proxy: str = None):
    if not load_config():          # also rebuilds the HTTP client with the saved proxy
        console.print("[red]No saved config found. Run without --resume first.[/]")
        sys.exit(1)
    if state.account_problem:
        logger.error("%s", state.account_problem)
        console.print(f"[red]{escape(state.account_problem)}. Add it with stakectl key add "
                      "or fix the lane.[/]")
        sys.exit(1)
    _sync_vault_account()
    if proxy and state.proxy != proxy:   # --proxy beats the saved one
        state.proxy = proxy
        _recreate_http()
    console.print("[dim]Loading saved config...[/]")
    game_label = GAMES.get(state.game, {}).get("label", state.game)
    try:
        api_test_connection()
        # Fetch real balance
        balances = api_get_balances()
        for b in balances:
            if b["currency"] == state.currency:
                bal = b["amount"]
                state.start_balance = bal
                state.current_balance = bal
                state.highest_balance = bal
                state.lowest_balance = bal
                break
        console.print(f"[green]Resumed -- {game_label} / {state.strategy} on {state.currency.upper()} "
                      f"@ {state.base_bet:.8f} base bet[/]")
        if state.start_balance > 0:
            console.print(f"  [dim]Balance:[/] {state.start_balance:.8f} {state.currency.upper()}")
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Connection failed{detail}. Check your settings and try again.[/]")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Stake AutoBot v" + VERSION + " -- Multi-Game")
    parser.add_argument("--version", action="version", version=f"Stake AutoBot v{VERSION}")
    parser.add_argument("--resume",  action="store_true", help="Skip wizard, reuse saved config")
    parser.add_argument("--daemon",  action="store_true", help="Run in background (no TUI, implies --resume)")
    parser.add_argument("--setup-only", action="store_true", help="Run wizard, save config, don't start betting")
    parser.add_argument("--monitor", action="store_true", help="Attach live TUI to running daemon")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status",  action="store_true", help="Show status of running session")
    group.add_argument("--stop",    action="store_true", help="Stop a running daemon session")
    group.add_argument("--run-reset", action="store_true",
                       help="Start a new run total for the run-wide profit increment "
                            "(all lanes, or --lane K)")
    group.add_argument("--list-presets", action="store_true", help="List saved presets")
    group.add_argument("--stats",   action="store_true", help="Show all-time session statistics")
    group.add_argument("--analytics", action="store_true",
                       help="Show lifetime analytics: aggregates over all sessions with "
                            "breakdowns by game, strategy and lane. Complement to --stats.")
    group.add_argument("--last-bets", type=int, metavar="N", help="Show last N bets from database")
    group.add_argument("--session-bets", type=int, metavar="ID", help="Show bets for a specific session")
    group.add_argument("--config-show", action="store_true",
                       help="Show the saved config as a table (no wizard, no side effects)")
    group.add_argument("--config-set", nargs="+", metavar="KEY=VALUE",
                       help="Change saved config without the wizard; a running daemon reloads "
                            "live-safe keys (SIGHUP). Aliases match the T hotkey (delay=0.35, "
                            "maxprofit=0.005, dbmode=sync, ...). Credentials are refused.")
    group.add_argument("--cleanup", nargs="?", type=int, metavar="DAYS", const=-1,
                       help="Purge bet rows of sessions older than DAYS (default: purge_days, "
                            "clamped 1-30). Session rows are kept.")
    group.add_argument("--benchmark", nargs="?", type=int, metavar="N", const=20,
                       help="API latency probe: N read-only GraphQL UserBalances calls "
                            "(default 20, clamped 3-200). No bets placed.")
    group.add_argument("--diagnose", action="store_true",
                       help="Bottleneck analysis of the running session (reads the state file)")
    group.add_argument("--lanes-status", action="store_true",
                       help="One row per running lane (state, session, profit, equity, bps) + totals")
    group.add_argument("--lane-ids", nargs="?", const="enabled", choices=("enabled", "all"),
                       help="Print the enabled lane ids (or every registry id with 'all')")
    group.add_argument("--lane-add", metavar="NAME",
                       help="Add one lane named NAME; the other lanes keep betting")
    group.add_argument("--lane-remove", type=int, metavar="K",
                       help="Remove lane K from the registry (lane 1 can't be removed)")
    group.add_argument("--lane-drain", nargs=2, metavar=("K", "ACTION"),
                       help="At lane K's session end: restart, remove, or cancel a request")
    group.add_argument("--key-list", action="store_true", help="List the vault keys (no secrets)")
    group.add_argument("--key-put", metavar="ID",
                       help="Add vault entry ID; the secret is read from stdin (an account: "
                            "access token, lockdown token and cookie, one per line)")
    group.add_argument("--key-rotate", metavar="ID", help="Replace vault entry ID; the secret is read from stdin")
    group.add_argument("--key-delete", metavar="ID", help="Delete vault entry ID (refused while a lane uses it)")
    # Modifiers, not commands: they combine WITH --stats / --analytics, so they
    # stay outside the mutually-exclusive group (WolfBet v2.30.9).
    parser.add_argument("--since", metavar="DATE",
                        help="With --stats/--analytics: window start, server-local time "
                             "(YYYY-MM-DD, 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS')")
    parser.add_argument("--until", metavar="DATE",
                        help="With --stats/--analytics: window end, same formats "
                             "(bare date = inclusive end of day)")
    parser.add_argument("--lane", type=int, metavar="K", default=None,
                        help=f"Parallel lane to run or inspect (1-{LANE_ID_MAX}, default 1): "
                             "--daemon / --status / --monitor / --stop / --diagnose act on "
                             "that lane; with --stats/--analytics only its sessions")
    parser.add_argument("--key-label", metavar="LABEL", help="With --key-put: a label (default: the id)")
    parser.add_argument("--key-kind", choices=("account", "proxy", "telegram"), default="account",
                        help="With --key-put: what the entry holds (default: account)")
    parser.add_argument("--lane-from", type=int, metavar="K", default=None,
                        help="With --lane-add: copy lane K's overrides (default: lane 1, none)")
    parser.add_argument("--lanes", type=int, metavar="M", default=None,
                        help=f"With --benchmark: probe at 1..M concurrent clients (2-{LANES_MAX}) "
                             "and recommend a lane count")
    parser.add_argument("--preset", type=str, metavar="NAME",
                        help="Load a named preset and start (skips wizard)")
    parser.add_argument("--proxy", type=str, metavar="URL",
                        help="Proxy URL: http://user:pass@host:port or socks5://host:port")
    parser.add_argument("--api-base", type=str, metavar="URL",
                        help="Override stake.com base with a Cloudflare Worker proxy URL "
                             "(e.g. https://my-bot.example.workers.dev). See cloudflare-worker/proxy.js.")
    parser.add_argument("--export", type=str, metavar="PATH",
                        help="Export a session to PATH (.json = JSON, else CSV)")
    parser.add_argument("--export-session-id", type=int, metavar="ID",
                        help="Session ID for --export (default: latest)")
    parser.add_argument("--backtest", action="store_true",
                        help="Run the strategy offline: no API calls, no Cloudflare, no real balance")
    parser.add_argument("--replay-session", type=int, metavar="ID",
                        help="With --backtest: replay a stored session's outcomes")
    parser.add_argument("--simulate", type=int, metavar="N",
                        help="With --backtest: simulate N bets (default 1000)")
    parser.add_argument("--seed", type=int, metavar="N",
                        help="With --simulate: RNG seed for a repeatable run")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging (dev mode)")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # v1.13.0 (wolfbet v2.35.0) -- pick the lane before any command so status /
    # monitor / stop / diagnose / the daemon all use that lane's files.
    try:
        _apply_lane(args.lane)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(2)

    if args.export:
        sys.exit(cmd_export(args.export, args.export_session_id))
    if args.backtest:
        sys.exit(_run_backtest(args))

    if args.proxy:
        try:
            args.proxy = validate_proxy(args.proxy) or ""
        except ValueError as e:
            console.print(f"[red]--proxy: {e}[/]")
            sys.exit(2)
        state.proxy = args.proxy
        _recreate_http()


    if args.api_base:
        global API_BASES, API_BASE
        API_BASES = [args.api_base.rstrip("/") + "/_api/casino"]
        API_BASE = API_BASES[0]
        console.print(f"[dim]API base override: {API_BASE}[/]")

    if args.daemon:
        args.resume = True

    if args.status:
        cmd_status()
        return
    if args.stop:
        cmd_stop()
        return
    if args.run_reset:
        cmd_run_reset(args.lane)
        return
    if args.list_presets:
        cmd_list_presets()
        return
    if args.stats:
        cmd_stats(since=args.since, until=args.until, lane=args.lane)
        return
    if args.analytics:
        cmd_analytics(since=args.since, until=args.until, lane=args.lane)
        return
    if args.monitor:
        cmd_monitor()
        return
    if args.last_bets:
        cmd_last_bets(args.last_bets)
        return
    if args.session_bets is not None:
        cmd_session_bets(args.session_bets)
        return
    if args.config_show:
        cmd_config_show()
        return
    if args.config_set:
        cmd_config_set(args.config_set)
        return
    if args.cleanup is not None:
        cmd_cleanup(None if args.cleanup == -1 else args.cleanup)
        return
    if args.benchmark is not None:
        cmd_benchmark(args.benchmark, proxy=args.proxy, lanes=args.lanes)
        return
    if args.diagnose:
        cmd_diagnose()
        return
    if args.lanes_status:
        cmd_lanes_status()
        return
    if args.lane_ids:
        reg = lane_registry(_read_config_raw() or {})
        print(" ".join(str(k) for k in sorted(reg) if args.lane_ids == "all" or reg[k]["enabled"]))
        return
    if args.lane_add is not None:
        sys.exit(cmd_lane_add(args.lane_add, args.lane_from))
    if args.lane_remove is not None:
        sys.exit(cmd_lane_remove(args.lane_remove))
    if args.lane_drain is not None:
        k, action = args.lane_drain
        if not k.isdigit() or action not in ("restart", "remove", "cancel"):
            console.print("[red]Usage: --lane-drain K restart|remove|cancel[/]")
            sys.exit(2)
        sys.exit(cmd_lane_drain(int(k), action))
    if args.key_list:
        sys.exit(cmd_key("list"))
    for action, key_id in (("put", args.key_put), ("rotate", args.key_rotate),
                           ("delete", args.key_delete)):
        if key_id is not None:
            sys.exit(cmd_key(action, key_id, args.key_label, args.key_kind))

    if args.setup_only:
        init_db()
        setup_wizard()
        console.print("[bold green]Config saved.[/] Start with: [cyan]python3 stake.py --resume[/] or [cyan]python3 stake.py --daemon[/]")
        return

    init_db()

    if args.preset:
        if not load_preset(args.preset):
            console.print(f"[red]Preset '{args.preset}' not found.[/]")
            cmd_list_presets()
            sys.exit(1)
        saved = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
        if not state.access_token:
            login = _active_account(saved)
            if login and login.get("access_token"):
                for f, v in login.items():
                    if f in ACCOUNT_FIELDS:
                        setattr(state, f, v)
            else:
                console.print("[red]No access token found. Run wizard first.[/]")
                sys.exit(1)
        # v1.13.0 -- a preset run still counts the saved lanes (count / split);
        # lane overrides are not applied on top of an explicit preset.
        state.lanes_count, state.lane_split = lanes_count(saved), lane_split(saved)
        if saved.get("proxy") and not args.proxy:
            # v1.12.0 fix: presets carry no proxy; use the saved one like --resume does
            _set_config_value("proxy", saved["proxy"])
            _recreate_http()
        game_label = GAMES.get(state.game, {}).get("label", state.game)
        console.print(f"[green]Loaded preset '{args.preset}' -- {game_label} / {state.strategy} on "
                      f"{state.currency.upper()} @ {state.base_bet:.8f}[/]")
        try:
            api_test_connection()
        except Exception as e:
            detail = f": {e}" if APP_ENV != "production" else ""
            console.print(f"[red]Connection failed{detail}. Check your settings and try again.[/]")
            sys.exit(1)

    elif args.resume or args.daemon:
        _load_and_connect(proxy=args.proxy)
    else:
        setup_wizard()

    if state.proxy and not _HAS_CFFI:
        console.print("[yellow]Proxy set but curl_cffi is not installed -- the proxy is ignored. "
                      "Install it: pip install curl_cffi[/]")

    # v1.13.0 (wolfbet v2.35.0) -- fix the lane's budget share, then stagger
    # lane K's first bet by (K-1)*2 s so lanes don't start in lock-step.
    if split_budget_on(state.lanes_count, state.lane_split) and state.current_balance <= 0:
        _refresh_balance()          # --preset never reads the balance otherwise
    _init_lane_budget()
    if state.lane > 1:
        time.sleep((state.lane - 1) * 2.0)

    _init_run()           # v1.15.0 -- restore the run-wide increment (scope run)
    state.session_id    = _db_start_session()
    _attach_run(state.session_id)
    state.session_start = time.time()
    state.run_started_at = state.session_start
    state.initial_multiplier = state.multiplier_target
    state.running       = True

    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    logger.warning("SESSION START  id=%s  game=%s  mode=%s  currency=%s  strategy=%s  "
                   "multiplier=%s  base_bet=%.8f  balance=%.8f",
                   state.session_id, state.game,
                   "daemon" if args.daemon else ("preset" if args.preset else "tui"),
                   state.currency, state.strategy,
                   state.multiplier_target, state.base_bet,
                   state.current_balance)

    def _shutdown(sig, frame):
        state.running = False
        state.status  = "Interrupted -- saving session..."
        logger.warning("SESSION END (signal)  bets=%d  profit=%+.8f  balance=%.8f",
                    state.total_bets, state.profit, state.current_balance)
        _stop_bet_writer(timeout=5.0)   # v1.12.0 -- drain queued bet rows
        _db_save_session()
        _save_state_file()
        _cleanup_state_file()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if hasattr(signal, "SIGUSR1"):
        def _sig_pause(sig, frame):
            state.paused = True
            state.status = "PAUSED (remote)"
        signal.signal(signal.SIGUSR1, _sig_pause)
    if hasattr(signal, "SIGUSR2"):
        def _sig_resume(sig, frame):
            state.paused = False
            state.status = "Resumed (remote)"
        signal.signal(signal.SIGUSR2, _sig_resume)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _sig_reload)

    # v1.12.0 -- startup retention purge (WolfBet v2.26.0)
    try:
        purged = cleanup_old_bets(DB_PATH, bet_age_days=state.purge_days)
        if purged:
            logger.info("STARTUP RETENTION purged %d old bet rows (>%dd)", int(purged), state.purge_days)
    except Exception as e:
        logger.warning("startup retention purge failed: %s", e)

    # v1.12.0 -- async bet writer (WolfBet v2.27.0)
    if state.db_write_mode == "async":
        _start_bet_writer()
        logger.info("BET WRITER started (async, batch=%d)", _BET_WRITER_BATCH_SIZE)

    t_bet = threading.Thread(target=betting_loop, daemon=True, name="BettingLoop")
    t_bet.start()

    t_retention = threading.Thread(target=_retention_worker, daemon=True, name="Retention")
    t_retention.start()

    def periodic_save():
        while state.running or state.paused:
            time.sleep(5)
            _db_save_session()
            _save_state_file()
    t_save = threading.Thread(target=periodic_save, daemon=True, name="PeriodicSave")
    t_save.start()

    # -- DAEMON MODE --
    if args.daemon:
        console.print(f"[bold green]Daemon started[/]  PID={os.getpid()}  Session #{state.session_id}  Game: {state.game}")
        console.print(f"[dim]  Check status:  python3 stake.py --status[/]")
        console.print(f"[dim]  Stop:          python3 stake.py --stop[/]")
        console.print(f"[dim]  Logs:          tail -f ~/.stake_logs/stake.log[/]")
        try:
            while state.running:
                time.sleep(2)
                _save_state_file()
        except KeyboardInterrupt:
            pass
        state.running = False
        _stop_bet_writer(timeout=5.0)
        _db_save_session()
        logger.warning("DAEMON EXIT  session=%d  bets=%d  profit=%+.8f  reason=%s",
                       state.session_id, state.total_bets, state.profit, state.status)
        _save_state_file()
        _cleanup_state_file()
        _print_summary()
        return

    # -- TUI MODE --
    t_input = threading.Thread(target=input_handler, daemon=True, name="InputHandler")
    t_input.start()

    _old_term = None
    try:
        import termios
        _old_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    sys.stdout.write("\033[?1049h")
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while state.running or state.paused:
            sys.stdout.write("\033[H")
            sys.stdout.write(build_dashboard_screen())
            sys.stdout.flush()
            time.sleep(0.5)

        sys.stdout.write("\033[H")
        sys.stdout.write(build_dashboard_screen())
        sys.stdout.flush()
        time.sleep(1.0)
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.write("\033[?1049l")
        sys.stdout.write("\033[0m")
        sys.stdout.flush()
        if _old_term is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _old_term)
            except Exception:
                pass
        import subprocess
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
        except Exception:
            pass
        sys.stdout.write("\033c")
        sys.stdout.flush()

    _stop_bet_writer(timeout=5.0)
    _db_save_session()
    _cleanup_state_file()
    _print_summary()


if __name__ == "__main__":
    main()
