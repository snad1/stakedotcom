"""Configuration — environment variables, constants, tier definitions."""

import os
import logging

# ── Environment ──────────────────────────────────────────
APP_ENV   = os.environ.get("APP_ENV", "production")
BOT_TOKEN = os.environ.get("STAKE_TG_TOKEN", "")
DATA_DIR  = os.environ.get("STAKE_TG_DATA", os.path.expanduser("~/.stakebot_tg"))
WEB_URL   = os.environ.get("STAKE_WEB_URL", "")          # e.g. http://IP:8001
WEB_SECRET = os.environ.get("STAKE_WEB_SECRET_KEY", "")   # must match web app

# ── API bases (stake.bet has lighter Cloudflare) ─────────
API_BASES = [
    "https://stake.bet/_api/casino",
    "https://stake.com/_api/casino",
]

# FlareSolverr endpoint for CF challenge solving
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")

# Cloudflare cookie cache
CF_CACHE_TTL = 1800  # 30 min

# Minimum bet on Stake
MIN_BET = 0.0001

# ── Rate limit tiers: seconds between bets ───────────────
TIERS = {
    "free":  1.0,    # 1 bet/sec
    "trial": 0.0,    # max speed
}

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stakebot")

# Suppress noisy httpx/httpcore debug logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Config keys persisted per user ───────────────────────
CONFIG_KEYS = [
    "access_token", "lockdown_token", "cookie",
    "currency", "game",
    "multiplier_target", "dice_target", "dice_condition",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets",
    "proxy",
]

# ── Supported currencies on Stake ─────────────────────────
CURRENCIES = [
    "btc", "eth", "ltc", "doge", "trx", "bch", "xrp",
    "usdt", "bnb", "ada", "matic",
]

# ── Available games ───────────────────────────────────────
GAME_LABELS = {
    "limbo": "Limbo",
    "dice":  "Dice",
}


def get_user_tier(user_id: int) -> str:
    """Return rate-limit tier for a user. Extensible for paid tiers."""
    return "trial"
