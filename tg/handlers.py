"""Telegram command handlers — all bot commands and callback queries.

Stake-specific differences from wolfbet:
  - Auth: access_token + lockdown_token + optional cookie (not single API key)
  - Multi-game: /set game limbo|dice, game-specific params (multiplier, dice target/condition)
  - No WolfRider strategy
  - /settoken command instead of /setkey
  - Session query includes game column (27 cols vs 26)
  - Bets query includes game + result_display columns
"""

import os
import json
import time
import asyncio
import sqlite3
from datetime import datetime
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from . import VERSION
from .config import (
    APP_ENV, CONFIG_KEYS, TIERS, CURRENCIES, GAME_LABELS, API_BASES,
    DATA_DIR, get_user_tier, logger,
)
from .database import (
    user_db_path, load_user_config, save_user_config,
    load_presets, save_presets,
)
from core.database import init_db, cleanup_old_bets
from core.strategy import (
    STRATEGIES, STRATEGY_NAMES, STRATEGY_BY_NAME, StrategyRule,
    describe_rule, load_rules_from_text,
)
from .engine import BettingEngine
from .formatter import (
    format_status, format_stop, format_milestone, format_session_row,
    format_session_detail, format_all_time, format_lastbets,
)


# ── Active engines (multiple per user) ───────────────────
# user_id → {slot: BettingEngine}
active_engines: Dict[int, Dict[int, BettingEngine]] = {}
_engine_chat_ids: Dict[int, int] = {}  # user_id → chat_id for resume
_next_slot: Dict[int, int] = {}  # user_id → next slot number

MAX_SESSIONS = 5  # max concurrent sessions per user

# ── Recurring bet state ─────────────────────────────────
# recurring_state[user_id][slot] = {"delay": int, "chat_id": int, "config": dict, "timer_task": Task|None}
recurring_state: Dict[int, Dict[int, dict]] = {}

# ── Active monitors ─────────────────────────────────────
# user_id → {slot: asyncio.Task}
active_monitors: Dict[int, Dict[int, asyncio.Task]] = {}

# ── Recurring notification throttle ──────────────────────
# Prevents Telegram flood bans during fast recurring cycles.
# user_id → {"stop": float, "start": float}  (unix timestamps)
_recurring_notify_ts: Dict[int, Dict[str, float]] = {}
RECURRING_NOTIFY_THROTTLE = 30  # max one stop + one start notification per 30s per user


def _throttle_recurring(user_id: int, kind: str) -> bool:
    """Return True (and record timestamp) if enough time has passed to send this notification."""
    now = time.time()
    user_ts = _recurring_notify_ts.setdefault(user_id, {})
    if now - user_ts.get(kind, 0) >= RECURRING_NOTIFY_THROTTLE:
        user_ts[kind] = now
        return True
    return False


def _get_running(user_id: int) -> Dict[int, BettingEngine]:
    """Return {slot: engine} for running engines only."""
    return {s: e for s, e in active_engines.get(user_id, {}).items() if e.running}


def _resolve_engine(user_id: int, args: list) -> tuple:
    """Resolve which engine a command targets.
    Returns (slot, engine, error_message).
    Single session auto-resolves. Multiple requires slot arg.
    """
    running = _get_running(user_id)
    if not running:
        return None, None, "No session running."
    if len(running) == 1:
        slot, engine = next(iter(running.items()))
        return slot, engine, None
    # Multiple — check if slot specified
    if args:
        try:
            slot = int(args[0])
            if slot in running:
                return slot, running[slot], None
            return None, None, f"No session in slot {slot}. Active: {', '.join(f'#{s}' for s in sorted(running))}"
        except ValueError:
            pass
    slots = ", ".join(f"#{s}" for s in sorted(running))
    return None, None, f"Multiple sessions running ({slots}). Specify slot number."


def _alloc_slot(user_id: int) -> int:
    """Allocate the next available slot for a user."""
    slot = _next_slot.get(user_id, 1)
    existing = active_engines.get(user_id, {})
    while slot in existing:
        slot += 1
    _next_slot[user_id] = slot + 1
    return slot


# ── Helper: reply via message or callback query ──────────
async def _reply(update: Update, text: str, **kwargs):
    """Reply via message or callback query, whichever is available."""
    if update.message:
        await update.message.reply_text(text, **kwargs)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, **kwargs)


# ── /start ───────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"\U0001f3b0 *Stake Bot v{VERSION}*\n\n"
        "Multi-game auto-betting engine for Stake via Telegram.\n"
        "Supports: Limbo, Dice\n\n"
        "*Setup:*\n"
        "/settoken — Set your Stake access tokens\n"
        "/balance — Check your balances\n"
        "/config — View current configuration\n\n"
        "*Configure:*\n"
        "/set currency usdt\n"
        "/set game limbo\n"
        "/set strategy martingale\n"
        "/set multiplier 2.0\n"
        "/set basebet 0.0001\n"
        "/set maxprofit 0.01\n\n"
        "*Session:*\n"
        "/bet — Start betting session (up to 5 concurrent)\n"
        "/stop — Stop session (`/stop all` for all)\n"
        "/stop recurring — Cancel all recurring bets\n"
        "/pause / /resume (add slot# for multi-session)\n"
        "/status — Live status (`/status 2` for slot)\n"
        "/monitor — Auto-updating status\n"
        "/stats — Session history\n"
        "/session — Session detail\n\n"
        "/strategies — List all strategies\n"
        "/help — Full command reference",
        parse_mode="Markdown",
    )


# ── /help ────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands:*\n\n"
        "*Setup*\n"
        "/settoken — Access tokens (message auto-deleted)\n"
        "/balance — Show all balances\n"
        "/config — Show current config\n"
        "/set — Set parameter\n"
        "/strategies — List strategies\n\n"
        "*Parameters for* /set*:*\n"
        "`currency` — usdt, btc, eth, ltc, doge, trx, bch, xrp, bnb, ada, matic\n"
        "`game` — limbo, dice\n"
        "`strategy` — flat, martingale, antimartingale, dalembert, paroli, delaymartingale, rulebased\n"
        "`multiplier` — target multiplier (e.g. 2.0)\n"
        "`basebet` — base bet amount\n"
        "`dicetarget` — dice target (0-100)\n"
        "`dicecondition` — above / below\n"
        "`lossmult` — loss multiplier (default 2.0)\n"
        "`winmult` — win multiplier (default 1.0)\n"
        "`delay` — seconds between bets\n"
        "`maxprofit` — stop at profit\n"
        "`maxloss` — stop at loss\n"
        "`maxbets` — stop after N bets\n"
        "`maxwins` — stop after N wins\n"
        "`minbalance` — stop below balance\n"
        "`delaythreshold` — Delay Martingale threshold\n"
        "`milestonebets` — Notify every N bets (0=off)\n"
        "`milestonewins` — Notify every N wins (0=off)\n"
        "`milestonelosses` — Notify every N losses (0=off)\n"
        "`milestoneprofit` — Notify every X profit (0=off)\n"
        "`profitthreshold` — Auto-raise base bet every X profit (`off` to disable)\n"
        "`profitincrement` — Amount to add to base bet (`off` to disable)\n"
        "`proxy` — http://user:pass@ip:port or socks5://... (`off` to disable)\n"
        "`recurring` — Auto-restart on stop condition (`on`/`off`)\n"
        "`recurringdelay` — Seconds before restart (default 60)\n"
        "`streakdelay_loss` — Delay after N losses (`5:1.0` = every 5 losses → 1s)\n"
        "`streakdelay_win` — Delay after N wins (`10:0.5` = every 10 wins → 0.5s)\n"
        "`streakbet_loss` — Scale bet after N losses (`10:0.5` = every 10 losses → halve)\n"
        "`basebet_pct` — Base bet as fraction of balance (`0.001` = 0.1%, `off` to disable)\n"
        "`purgedays` — Auto-delete bets older than N days (1-30, default 1)\n\n"
        "*Session* _(up to 5 concurrent)_\n"
        "/bet — Start session\n"
        "/stop — Stop session (`/stop all` or `/stop 2`)\n"
        "/stop recurring — Cancel all recurring bets and stop sessions\n"
        "/tweak — Change settings on a running session\n"
        "/pause — Pause (`/pause all` or `/pause 2`)\n"
        "/resume — Resume (`/resume all` or `/resume 2`)\n"
        "/status — Live status (`/status 2` for specific slot)\n"
        "/monitor — Auto-updating status (default 5s)\n"
        "/stats — Session history\n"
        "/session — Detailed session report\n"
        "/lastbets — Recent bets\n\n"
        "*Rules*\n"
        "/rules — List current rules\n"
        "/addrule — Add rule (JSON)\n"
        "/delrule — Delete rule by number\n"
        "/editrule — Edit rule by number (JSON patch)\n"
        "/clearrules — Clear all rules\n\n"
        "*Presets*\n"
        "/presets — List presets\n"
        "/savepreset — Save current config\n"
        "/loadpreset — Load preset\n\n"
        "*Maintenance*\n"
        "/cleanup — Purge old bet records (`/cleanup 3` for 3 days)\n"
        "/delsession — Delete a session by ID\n\n"
        "*Web*\n"
        "/web — Open web dashboard\n"
        "/benchmark — Test API response speed",
        parse_mode="Markdown",
    )


# ── /settoken ────────────────────────────────────────────
async def cmd_settoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    if not context.args or len(context.args) < 2:
        await update.effective_chat.send_message(
            "Usage: /settoken <access\\_token> <lockdown\\_token> [cookie]\n\n"
            "Get these from Stake browser DevTools (Network tab).\n"
            "Your message is auto-deleted for security.",
            parse_mode="Markdown")
        return

    user_id = update.effective_user.id
    config = load_user_config(user_id)
    config["access_token"] = context.args[0]
    config["lockdown_token"] = context.args[1]
    if len(context.args) > 2:
        raw_cookie = " ".join(context.args[2:])
        config["cookie"] = "" if raw_cookie.lower() == "none" else raw_cookie
    save_user_config(user_id, config)

    await update.effective_chat.send_message(
        "Tokens saved. Your message was deleted for security.\n"
        "Test with /balance", parse_mode="Markdown")


# ── /benchmark ───────────────────────────────────────────
async def cmd_benchmark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run test bets (amount=0) and measure API response times."""
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    if not config.get("access_token"):
        await update.message.reply_text("Set your token first: /settoken", parse_mode="Markdown")
        return

    count = 10
    if context.args:
        try:
            count = min(int(context.args[0]), 50)
        except ValueError:
            pass

    msg = await update.message.reply_text(f"Running {count} test bets (amount=0)...")

    import time as _time

    db_path = user_db_path(user_id)
    test_config = dict(config)
    test_config["base_bet"] = 0
    engine = BettingEngine(user_id, db_path, test_config)

    await engine._recreate_http()
    if not await engine._api_test_connection():
        detail = f": {engine.last_error}" if APP_ENV != "production" else ""
        await msg.edit_text(f"Connection failed{detail}. Check /config and try again.")
        return

    times = []
    for i in range(count):
        try:
            t0 = _time.time()
            result = await engine._api_place_bet(0)
            elapsed = (_time.time() - t0) * 1000
            times.append(elapsed)
        except Exception:
            times.append(-1)

    valid = [t for t in times if t > 0]
    if not valid:
        await msg.edit_text("All requests failed.")
        return

    avg = sum(valid) / len(valid)
    mn, mx = min(valid), max(valid)
    failed = len(times) - len(valid)
    theoretical_bps = 1000 / avg if avg > 0 else 0

    lines = [
        f"*Benchmark Results ({count} bets)*\n",
        f"  Avg: `{avg:.0f}ms`",
        f"  Min: `{mn:.0f}ms`",
        f"  Max: `{mx:.0f}ms`",
        f"  Failed: `{failed}`\n",
        f"  Max theoretical: `{theoretical_bps:.1f}` bets/sec",
        f"\n*Individual times:*",
        f"`{'  '.join(f'{t:.0f}ms' if t > 0 else 'FAIL' for t in times)}`",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


# ── /balance ─────────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    if not config.get("access_token"):
        await update.message.reply_text(
            "Set your tokens first: /settoken <access> <lockdown>",
            parse_mode="Markdown")
        return

    await update.message.reply_text("Fetching balances…")
    try:
        db_path = user_db_path(user_id)
        engine = BettingEngine(user_id, db_path, config)
        await engine._recreate_http()
        engine._cf_cache_load()
        balances = []
        for base in API_BASES:
            engine._api_base = base
            balances = await engine.api_get_balances()
            if balances:
                break
            site_url = base.split("/_api")[0]
            if await engine._solve_cloudflare(site_url):
                balances = await engine.api_get_balances()
                if balances:
                    break
        if not balances:
            await update.message.reply_text(
                "No balances found or tokens invalid.\n"
                "Check your tokens with /settoken")
            return

        lines = ["*Balances:*"]
        for b in balances:
            cur = b.get("currency", "?").upper()
            amt = float(b.get("amount", 0))
            if amt > 0:
                lines.append(f"`{cur:>6}  {amt:.8f}`")
        if len(lines) == 1:
            lines.append("_All balances are zero_")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_balance error: %s", e, exc_info=True)
        msg = f"Error: `{e}`" if APP_ENV != "production" else "Something went wrong. Please try again."
        await update.message.reply_text(msg, parse_mode="Markdown")


# ── /config ──────────────────────────────────────────────
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)

    def _cv(key, default, typ=float):
        v = config.get(key)
        return typ(v) if v is not None else typ(default)

    try:
        strategy_key = config.get("strategy_key") or "2"
        strategy = config.get("strategy") or STRATEGY_NAMES.get(strategy_key, "Martingale")
        game = config.get("game") or "limbo"
        multiplier = _cv("multiplier_target", 2.0)
        wc = round(99.0 / multiplier, 2) if multiplier > 0 else 0

        lines = [
            "*Current Configuration:*",
            f"Currency: `{(config.get('currency') or 'usdt').upper()}`",
            f"Game: `{GAME_LABELS.get(game, game)}`",
            f"Strategy: `{strategy}`",
            f"Multiplier: `{multiplier:.2f}x` ({wc}% win chance)",
            f"Base bet: `{_cv('base_bet', 0.0001):.8f}`",
        ]

        if game == "dice":
            lines.append(f"Dice target: `{_cv('dice_target', 50.5)}`")
            lines.append(f"Dice condition: `{config.get('dice_condition') or 'above'}`")

        # Strategy-specific fields
        if strategy_key in ("2", "6"):  # Martingale, Delay Martingale
            lines.append(f"Loss mult: `{_cv('loss_mult', 2.0):.2f}x`")
        if strategy_key in ("3", "5"):  # Anti-Martingale, Paroli
            lines.append(f"Win mult: `{_cv('win_mult', 1.0):.2f}x`")
        if strategy_key == "6":  # Delay Martingale
            lines.append(f"Delay threshold: `{_cv('delay_martin_threshold', 3, int)}`")

        lines.append(f"Bet delay: `{_cv('bet_delay', 0)}s`")

        sdl = config.get("streak_delay_loss")
        sdw = config.get("streak_delay_win")
        sbl = config.get("streakbet_loss")
        bbp = config.get("basebet_pct")
        if sdl:
            lines.append(f"Streak delay loss: `{sdl}`")
        if sdw:
            lines.append(f"Streak delay win: `{sdw}`")
        if sbl:
            lines.append(f"Streak bet loss: `{sbl}`")
        if bbp:
            lines.append(f"Basebet pct: `{float(bbp)*100:.4f}%` of balance")

        # Stop conditions
        stops = []
        if config.get("max_profit"): stops.append(f"Max profit: {float(config['max_profit']):.8f}")
        if config.get("max_loss"):   stops.append(f"Max loss: {float(config['max_loss']):.8f}")
        if config.get("max_bets"):   stops.append(f"Max bets: {config['max_bets']}")
        if config.get("max_wins"):   stops.append(f"Max wins: {config['max_wins']}")
        if config.get("stop_on_balance"): stops.append(f"Min balance: {float(config['stop_on_balance']):.8f}")
        if stops:
            lines.append(f"Stop: `{', '.join(stops)}`")
        else:
            lines.append("Stop conditions: _none_")

        # Profit increment
        pi = config.get("profit_increment")
        pt = config.get("profit_threshold")
        if pi and pt:
            lines.append(f"Profit increment: `+{float(pi):.8f} every {float(pt):.8f} profit`")

        # Milestones
        ms = []
        mb = config.get("milestone_bets", 100)
        if mb: ms.append(f"{mb} bets")
        mw = config.get("milestone_wins", 0)
        if mw: ms.append(f"{mw} wins")
        ml = config.get("milestone_losses", 0)
        if ml: ms.append(f"{ml} losses")
        mp = config.get("milestone_profit", 0)
        if mp: ms.append(f"{mp} profit")
        lines.append(f"Milestones: `{', '.join(ms) if ms else 'off'}`")

        rec_on = config.get("recurring", False)
        rec_delay = int(config.get("recurring_delay") or 60)
        lines.append(f"Recurring: `{'on (' + str(rec_delay) + 's)' if rec_on else 'off'}`")

        # Rules (for rule-based strategy)
        if strategy_key == "7":
            rules_text = config.get("custom_rules_text", "")
            rules = load_rules_from_text(rules_text) if rules_text else []
            if rules:
                lines.append(f"\n*Rules ({len(rules)}):*")
                for i, r in enumerate(rules, 1):
                    lines.append(f"  `{i}.` {r.description}")
            else:
                lines.append("Rules: _none configured_")

        token_set = "Yes" if config.get("access_token") else "No"
        lines.append(f"Tokens: `{token_set}`")
        lines.append(f"Tier: `{get_user_tier(user_id)}`")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_config error: %s", e, exc_info=True)
        msg = f"Config error: `{e}`" if APP_ENV != "production" else "Configuration error. Please check your settings."
        await update.message.reply_text(msg, parse_mode="Markdown")


# ── /set ─────────────────────────────────────────────────
async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /set <param> <value>\nSee /help for parameters.",
            parse_mode="Markdown")
        return

    param = context.args[0].lower()
    value = " ".join(context.args[1:]).replace(",", "")
    config = load_user_config(user_id)

    try:
        if param == "currency":
            config["currency"] = value.lower()
        elif param == "game":
            g = value.lower()
            if g not in GAME_LABELS:
                await update.message.reply_text(
                    f"Unknown game. Available: {', '.join(GAME_LABELS.keys())}",
                    parse_mode="Markdown")
                return
            config["game"] = g
        elif param == "strategy":
            name = value.lower().replace("-", "").replace(" ", "").replace("_", "")
            key = STRATEGY_BY_NAME.get(name)
            if not key:
                if value in STRATEGIES:
                    key = value
                else:
                    await update.message.reply_text(
                        "Unknown strategy. Use /strategies to see options.",
                        parse_mode="Markdown")
                    return
            config["strategy_key"] = key
            config["strategy"] = STRATEGY_NAMES[key]
        elif param == "multiplier":
            mult = float(value)
            if mult <= 1.0:
                await update.message.reply_text("Multiplier must be > 1.0")
                return
            config["multiplier_target"] = mult
            wc = 99.0 / mult
            cond = config.get("dice_condition", "above")
            if cond == "above":
                config["dice_target"] = round(100.0 - wc, 2)
            else:
                config["dice_target"] = round(wc, 2)
        elif param == "basebet":
            val = float(value)
            if val < 0:
                await update.message.reply_text("Base bet must be >= 0.")
                return
            config["base_bet"] = val
        elif param == "dicetarget":
            config["dice_target"] = float(value)
        elif param in ("dicecondition", "condition"):
            if value.lower() not in ("above", "below"):
                await update.message.reply_text(
                    "Condition must be `above` or `below`", parse_mode="Markdown")
                return
            config["dice_condition"] = value.lower()
            # Auto-update dice target
            mult = config.get("multiplier_target", 2.0)
            wc = 99.0 / mult
            if value.lower() == "above":
                config["dice_target"] = round(100.0 - wc, 2)
            else:
                config["dice_target"] = round(wc, 2)
        elif param == "lossmult":
            val = float(value)
            if val < 0:
                await update.message.reply_text("Loss multiplier must be > 0.")
                return
            config["loss_mult"] = val
        elif param == "winmult":
            val = float(value)
            if val < 0:
                await update.message.reply_text("Win multiplier must be > 0.")
                return
            config["win_mult"] = val
        elif param == "delay":
            val = float(value)
            if val < 0:
                await update.message.reply_text("Delay must be >= 0.")
                return
            config["bet_delay"] = val
        elif param == "maxprofit":
            if value.lower() != "off":
                val = float(value)
                if val < 0:
                    await update.message.reply_text("Max profit must be > 0.")
                    return
                config["max_profit"] = val
            else:
                config["max_profit"] = None
        elif param == "maxloss":
            if value.lower() != "off":
                val = float(value)
                if val < 0:
                    await update.message.reply_text("Max loss must be > 0.")
                    return
                config["max_loss"] = val
            else:
                config["max_loss"] = None
        elif param == "maxbets":
            if value.lower() != "off":
                val = int(value)
                if val < 0:
                    await update.message.reply_text("Max bets must be > 0.")
                    return
                config["max_bets"] = val
            else:
                config["max_bets"] = None
        elif param == "maxwins":
            if value.lower() != "off":
                val = int(value)
                if val < 0:
                    await update.message.reply_text("Max wins must be > 0.")
                    return
                config["max_wins"] = val
            else:
                config["max_wins"] = None
        elif param == "minbalance":
            if value.lower() != "off":
                val = float(value)
                if val < 0:
                    await update.message.reply_text("Min balance must be >= 0.")
                    return
                config["stop_on_balance"] = val
            else:
                config["stop_on_balance"] = None
        elif param == "delaythreshold":
            config["delay_martin_threshold"] = int(value)
        elif param in ("milestonebets", "msbets"):
            config["milestone_bets"] = int(value)
        elif param in ("milestonewins", "mswins"):
            config["milestone_wins"] = int(value)
        elif param in ("milestonelosses", "mslosses"):
            config["milestone_losses"] = int(value)
        elif param in ("milestoneprofit", "msprofit"):
            config["milestone_profit"] = float(value)
        elif param in ("profitthreshold", "pt"):
            if value.lower() != "off":
                val = float(value)
                if val < 0:
                    await update.message.reply_text("Profit threshold must be > 0 (or 'off').")
                    return
                config["profit_threshold"] = val
            else:
                config["profit_threshold"] = None
        elif param in ("profitincrement", "pi"):
            if value.lower() != "off":
                val = float(value)
                if val < 0:
                    await update.message.reply_text("Profit increment must be >= 0 (or 'off').")
                    return
                config["profit_increment"] = val
            else:
                config["profit_increment"] = None
        elif param == "recurring":
            if value.lower() in ("on", "true", "yes", "1"):
                config["recurring"] = True
            elif value.lower() in ("off", "false", "no", "0"):
                config["recurring"] = False
            else:
                await update.message.reply_text("Use: `/set recurring on` or `/set recurring off`", parse_mode="Markdown")
                return
        elif param in ("recurringdelay", "recdelay"):
            val = int(value)
            if val < 5:
                await update.message.reply_text("Recurring delay must be >= 5 seconds.")
                return
            config["recurring_delay"] = val
        elif param in ("streakdelay_loss", "sdloss"):
            if value.lower() == "off":
                config["streak_delay_loss"] = None
            elif ":" not in value:
                await update.message.reply_text("Format: `/set streakdelay_loss 5:1.0` (every 5 losses → 1s delay)\nUse `off` to disable.", parse_mode="Markdown")
                return
            else:
                config["streak_delay_loss"] = value
        elif param in ("streakdelay_win", "sdwin"):
            if value.lower() == "off":
                config["streak_delay_win"] = None
            elif ":" not in value:
                await update.message.reply_text("Format: `/set streakdelay_win 10:0.5` (every 10 wins → 0.5s delay)\nUse `off` to disable.", parse_mode="Markdown")
                return
            else:
                config["streak_delay_win"] = value
        elif param in ("streakbet_loss", "sbloss"):
            if value.lower() == "off":
                config["streakbet_loss"] = None
            elif ":" not in value:
                await update.message.reply_text("Format: `/set streakbet_loss 10:0.5` (every 10 losses → halve bet)\nUse `off` to disable.", parse_mode="Markdown")
                return
            else:
                config["streakbet_loss"] = value
        elif param in ("basebet_pct", "bbpct"):
            if value.lower() == "off":
                config["basebet_pct"] = None
            else:
                try:
                    val = float(value)
                except ValueError:
                    await update.message.reply_text("basebet_pct must be a fraction (e.g. `0.001` = 0.1% of balance)", parse_mode="Markdown")
                    return
                if val < 0 or val > 1:
                    await update.message.reply_text("basebet_pct must be between 0 and 1.")
                    return
                config["basebet_pct"] = val
        elif param in ("purgedays", "purge_days"):
            val = int(value)
            if val < 1 or val > 30:
                await update.message.reply_text("Purge days must be 1-30.")
                return
            config["purge_days"] = val
        elif param == "proxy":
            if value.lower() in ("off", "none", "direct", "0"):
                config["proxy"] = None
                await update.message.reply_text("Proxy disabled — using direct connection.", parse_mode="Markdown")
                save_user_config(user_id, config)
                return
            if not (value.startswith("http://") or value.startswith("https://") or value.startswith("socks5://")):
                await update.message.reply_text(
                    "Proxy format: `http://user:pass@ip:port` or `socks5://user:pass@ip:port`\n"
                    "Use `/set proxy off` to disable.",
                    parse_mode="Markdown")
                return
            config["proxy"] = value
        else:
            await update.message.reply_text(
                f"Unknown parameter: `{param}`\nSee /help", parse_mode="Markdown")
            return

        save_user_config(user_id, config)
        await update.message.reply_text(f"Set `{param}` = `{value}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text(
            f"Invalid value for `{param}`: `{value}`", parse_mode="Markdown")


# ── /strategies ──────────────────────────────────────────
async def cmd_strategies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["*Available Strategies:*\n"]
    for k, (name, desc) in STRATEGIES.items():
        lines.append(f"`{k}` *{name}* — {desc}")
    lines.append("\nSet with: /set strategy <name>")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /bet ─────────────────────────────────────────────────
async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    running = _get_running(user_id)

    if len(running) >= MAX_SESSIONS:
        await update.message.reply_text(
            f"Max {MAX_SESSIONS} concurrent sessions. /stop a session first.")
        return

    config = load_user_config(user_id)
    if not config.get("access_token"):
        await update.message.reply_text(
            "Set your tokens first: /settoken <access> <lockdown>",
            parse_mode="Markdown")
        return

    # Apply tier rate limit
    tier = get_user_tier(user_id)
    min_delay = TIERS.get(tier, 1.0)
    config_delay = float(config.get("bet_delay") or 0)
    config["bet_delay"] = max(config_delay, min_delay)

    db_path = user_db_path(user_id)
    try:
        engine = BettingEngine(user_id, db_path, config)
    except Exception as e:
        logger.error("cmd_bet error: %s", e, exc_info=True)
        msg = f"Config error: `{e}`" if APP_ENV != "production" else "Configuration error. Please check your settings."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    slot = _alloc_slot(user_id)

    chat_id = update.effective_chat.id
    app = context.application

    def _make_on_stop(cid, uid, s):
        def on_stop(reason):
            t = asyncio.create_task(_notify_stop(cid, uid, s, reason, app))
            t.add_done_callback(_log_task_exception)
        return on_stop

    def _make_on_milestone(cid):
        def on_milestone(data):
            t = asyncio.create_task(_notify_milestone(cid, data, app))
            t.add_done_callback(_log_task_exception)
        return on_milestone

    def _make_on_error(cid):
        def on_error(msg):
            t = asyncio.create_task(_notify_error(cid, msg, app))
            t.add_done_callback(_log_task_exception)
        return on_error

    engine.on_stop = _make_on_stop(chat_id, user_id, slot)
    engine.on_milestone = _make_on_milestone(chat_id)
    engine.on_error = _make_on_error(chat_id)

    game_label = GAME_LABELS.get(engine.game, engine.game)
    msg = await update.message.reply_text(f"Connecting to Stake ({game_label})…")

    if not await engine.start():
        detail = f": {engine.last_error}" if APP_ENV != "production" else ""
        await msg.edit_text(f"Failed to start{detail}. Check /config and try again.")
        return

    active_engines.setdefault(user_id, {})[slot] = engine
    _engine_chat_ids[user_id] = chat_id

    if config.get("recurring"):
        delay = int(config.get("recurring_delay") or 60)
        recurring_state.setdefault(user_id, {})[slot] = {
            "delay": delay,
            "chat_id": chat_id,
            "config": dict(config),
            "timer_task": None,
            "app": app,
        }

    slot_info = f" (slot {slot})" if len(_get_running(user_id)) > 1 else ""
    rec_info = f"\nRecurring: `{int(config.get('recurring_delay') or 60)}s`" if config.get("recurring") else ""
    from .formatter import format_full_config
    cfg_lines = "\n".join(format_full_config(engine.get_status()))
    try:
        await msg.edit_text(
            f"Session #{engine.session_id}{slot_info} started!\n\n"
            f"\u2699\ufe0f *Config*\n{cfg_lines}\n\n"
            f"\U0001f4b0 Balance: `{engine.current_balance:.8f} {engine.currency.upper()}`"
            f"{rec_info}\n\n"
            f"Use /status to check progress.",
            parse_mode="Markdown",
        )
    except Exception:
        logger.warning("User %d: Session started but Telegram confirmation timed out", user_id)


async def _safe_send(app, chat_id, text, **kwargs):
    """Engine-driven send that swallows flood/network errors to avoid unraised Task exceptions."""
    from telegram.error import RetryAfter, NetworkError, TimedOut, BadRequest
    try:
        await _safe_send(app, chat_id, text, **kwargs)
    except RetryAfter as e:
        logger.warning("Telegram flood — dropping notification (retry in %ss)", e.retry_after)
    except (NetworkError, TimedOut) as e:
        logger.warning("Telegram network — dropping notification: %s", e)
    except BadRequest as e:
        logger.warning("Telegram rejected notification: %s", e)


def _log_task_exception(task: asyncio.Task):
    """Log exceptions from fire-and-forget tasks so they aren't silently swallowed."""
    if not task.cancelled() and task.exception():
        logger.error("Unhandled task exception: %s", task.exception(), exc_info=task.exception())


async def _notify_stop(chat_id: int, user_id: int, slot: int, reason: str, app):
    user_engines = active_engines.get(user_id, {})
    engine = user_engines.pop(slot, None)
    if not user_engines:
        active_engines.pop(user_id, None)
        _engine_chat_ids.pop(user_id, None)
    if not engine:
        return
    # Throttle stop notifications for fast recurring cycles to avoid Telegram flood bans.
    # Always notify on: manual stop, errors, insufficient balance, or net loss.
    # Throttle (max 1 per RECURRING_NOTIFY_THROTTLE seconds) for positive-profit condition stops.
    user_rec = recurring_state.get(user_id, {})
    is_recurring = bool(user_rec.get(slot))
    is_notable = (
        not is_recurring
        or reason == "Stopped by user"
        or "insufficient" in reason.lower()
        or engine.profit < 0
    )
    send_stop = is_notable or _throttle_recurring(user_id, "stop")

    if send_stop:
        try:
            s = engine.get_status()
            text = format_stop(s, reason)
            await _safe_send(app, chat_id, text, parse_mode="Markdown")
        except Exception as exc:
            logger.error("_notify_stop formatting error (slot %d): %s", slot, exc, exc_info=True)
            await _safe_send(app, chat_id, f"Session #{engine.session_id} stopped: {reason}")
    else:
        logger.info("User %d slot %d: stop notification throttled (profit %.8f, reason: %s)",
                    user_id, slot, engine.profit, reason)

    # Recurring bet: schedule restart if condition stop (not manual)
    rec = user_rec.get(slot)
    if rec and reason != "Stopped by user" and "insufficient" not in reason.lower():
        delay = rec["delay"]
        config = rec["config"]
        # Carry forward the current base_bet (profit increment may have raised it)
        config["base_bet"] = engine.base_bet
        await _safe_send(app, 
            chat_id,
            f"Recurring: restarting in {delay}s...\n"
            f"Use `/stop recurring` to cancel.",
            parse_mode="Markdown",
        )
        task = asyncio.create_task(
            _recurring_restart(chat_id, user_id, slot, delay, config, app))
        task.add_done_callback(_log_task_exception)
        rec["timer_task"] = task


async def _recurring_restart(chat_id: int, user_id: int, old_slot: int,
                             delay: int, config: dict, app):
    await asyncio.sleep(delay)

    user_rec = recurring_state.get(user_id, {})
    if old_slot not in user_rec:
        return  # cancelled while waiting

    running = _get_running(user_id)
    if len(running) >= MAX_SESSIONS:
        await _safe_send(app, 
            chat_id, "Recurring restart skipped — max sessions reached.")
        user_rec.pop(old_slot, None)
        return

    tier = get_user_tier(user_id)
    min_delay = TIERS.get(tier, 1.0)
    config_delay = float(config.get("bet_delay") or 0)
    config["bet_delay"] = max(config_delay, min_delay)

    db_path = user_db_path(user_id)
    try:
        engine = BettingEngine(user_id, db_path, config)
    except Exception as e:
        logger.error("Recurring restart config error: %s", e, exc_info=True)
        await _safe_send(app, chat_id, f"Recurring restart failed: config error.")
        user_rec.pop(old_slot, None)
        return

    # Reuse old_slot if free (keeps /monitor + callbacks bound to same slot)
    existing = active_engines.get(user_id, {})
    slot = old_slot if old_slot not in existing else _alloc_slot(user_id)

    def _make_on_stop(cid, uid, s):
        def on_stop(reason):
            t = asyncio.create_task(_notify_stop(cid, uid, s, reason, app))
            t.add_done_callback(_log_task_exception)
        return on_stop

    def _make_on_milestone(cid):
        def on_milestone(data):
            t = asyncio.create_task(_notify_milestone(cid, data, app))
            t.add_done_callback(_log_task_exception)
        return on_milestone

    def _make_on_error(cid):
        def on_error(msg):
            t = asyncio.create_task(_notify_error(cid, msg, app))
            t.add_done_callback(_log_task_exception)
        return on_error

    engine.on_stop = _make_on_stop(chat_id, user_id, slot)
    engine.on_milestone = _make_on_milestone(chat_id)
    engine.on_error = _make_on_error(chat_id)

    if not await engine.start():
        err = engine.last_error or "unknown error"
        logger.warning("Recurring restart failed for user %d slot %d: %s — retrying in %ds",
                       user_id, old_slot, err, delay)
        await _safe_send(app, chat_id,
                         f"⚠️ Recurring restart failed: {err}\nRetrying in {delay}s…")
        # Re-schedule retry — keep recurring state alive (don't pop)
        rec = user_rec.get(old_slot)
        if rec:
            retry_task = asyncio.create_task(
                _recurring_restart(chat_id, user_id, old_slot, delay, config, app))
            retry_task.add_done_callback(_log_task_exception)
            rec["timer_task"] = retry_task
        return

    active_engines.setdefault(user_id, {})[slot] = engine
    _engine_chat_ids[user_id] = chat_id

    # Move recurring state to new slot
    rec_data = user_rec.pop(old_slot, None)
    if rec_data:
        rec_data["timer_task"] = None
        user_rec[slot] = rec_data
        recurring_state.setdefault(user_id, {})[slot] = rec_data

    game_label = GAME_LABELS.get(engine.game, engine.game)
    wc = round(99.0 / engine.multiplier_target, 2)
    if _throttle_recurring(user_id, "start"):
        try:
            await _safe_send(app,
                chat_id,
                f"Recurring session #{engine.session_id} started!\n\n"
                f"Game: `{game_label}`\n"
                f"Strategy: `{engine.strategy}`\n"
                f"Multiplier: `{engine.multiplier_target:.2f}x` ({wc}%)\n"
                f"Base bet: `{engine.base_bet:.8f} {engine.currency.upper()}`\n"
                f"Balance: `{engine.current_balance:.8f} {engine.currency.upper()}`\n"
                f"Recurring: `{rec_data['delay'] if rec_data else delay}s`\n\n"
                f"Use /status to check progress.",
                parse_mode="Markdown",
            )
        except Exception:
            logger.warning("User %d: Session started but Telegram confirmation timed out", user_id)


async def _notify_milestone(chat_id: int, data: dict, app):
    text = format_milestone(data)
    await _safe_send(app, chat_id, text, parse_mode="Markdown")


async def _notify_error(chat_id: int, message: str, app):
    await _safe_send(app, chat_id, f"⚠️ {message}", parse_mode="Markdown")


def _cancel_recurring_for_slot(user_id: int, slot: int):
    user_rec = recurring_state.get(user_id, {})
    rec = user_rec.pop(slot, None)
    if rec:
        task = rec.get("timer_task")
        if task and not task.done():
            task.cancel()
    if not user_rec:
        recurring_state.pop(user_id, None)


def _cancel_recurring_for_user(user_id: int):
    user_rec = recurring_state.pop(user_id, {})
    for rec in user_rec.values():
        task = rec.get("timer_task")
        if task and not task.done():
            task.cancel()


# ── /stop ────────────────────────────────────────────────
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    running = _get_running(user_id)

    if context.args and context.args[0].lower() == "recurring":
        # Cancel all recurring bets and stop all sessions
        user_rec = recurring_state.pop(user_id, {})
        cancelled = 0
        for rec in user_rec.values():
            task = rec.get("timer_task")
            if task and not task.done():
                task.cancel()
                cancelled += 1
        if running:
            for engine in running.values():
                engine.stop_reason = "Stopped by user"
                engine.stop()
        config = load_user_config(user_id)
        config["recurring"] = False
        save_user_config(user_id, config)
        parts = []
        if cancelled:
            parts.append(f"Cancelled {cancelled} pending restart(s)")
        if running:
            parts.append(f"Stopping {len(running)} session(s)")
        parts.append("Recurring mode disabled")
        await _reply(update, ". ".join(parts) + ".")
        return

    # /stop all — stop all sessions
    if context.args and context.args[0].lower() == "all":
        _cancel_recurring_for_user(user_id)
        if running:
            for engine in running.values():
                engine.stop_reason = "Stopped by user"
                engine.stop()
            await _reply(update, f"Stopping {len(running)} session(s)...")
            return

    if running:
        slot, engine, err = _resolve_engine(user_id, context.args or [])
        if err:
            await _reply(update, err + "\nUse `/stop all` to stop all.", parse_mode="Markdown")
            return
        _cancel_recurring_for_slot(user_id, slot)
        engine.stop_reason = "Stopped by user"
        engine.stop()
        return

    # Clean up zombie sessions
    db_path = user_db_path(user_id)
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL")
        zombies = cur.fetchone()[0]
        if zombies:
            conn.execute(
                "UPDATE sessions SET ended_at = ? "
                "WHERE ended_at IS NULL", (datetime.now().isoformat(),))
            conn.commit()
            conn.close()
            await _reply(update, f"Cleaned up {zombies} stale session(s).")
            return
        conn.close()
    await _reply(update, "No session running.")


# ── /tweak ───────────────────────────────────────────────
async def cmd_tweak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change settings on a running session without stopping it."""
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "*Tweak a running session:*\n\n"
            "`/tweak delay 0.1` \u2014 bet delay\n"
            "`/tweak maxwins 5000` \u2014 stop at wins\n"
            "`/tweak maxbets 10000` \u2014 stop at bets\n"
            "`/tweak maxprofit 0.001` \u2014 stop at profit\n"
            "`/tweak maxloss 0.001` \u2014 stop at loss\n"
            "`/tweak minbalance 1.0` \u2014 stop below balance\n"
            "`/tweak milestonewins 1000` \u2014 milestone interval\n"
            "`/tweak basebet 0.00000002`\n"
            "`/tweak multiplier 2.5`\n"
            "`/tweak lossmult 2.01`\n"
            "`/tweak winmult 1.5`\n\n"
            "Use `off` to disable: `/tweak maxwins off`\n"
            "Add slot: `/tweak maxwins 5000 2`",
            parse_mode="Markdown")
        return

    param = context.args[0].lower()
    value = context.args[1]
    slot_args = context.args[2:] if len(context.args) > 2 else []

    slot, engine, err = _resolve_engine(user_id, slot_args)
    if err:
        await _reply(update, err)
        return

    off = value.lower() in ("off", "none", "0", "disable")
    changes = {}
    try:
        if param in ("basebet", "base_bet"):
            changes["base_bet"] = float(value)
        elif param == "delay":
            changes["bet_delay"] = float(value)
        elif param in ("sdloss", "streakdelay_loss"):
            changes["streak_delay_loss"] = None if off else value
        elif param in ("sdwin", "streakdelay_win"):
            changes["streak_delay_win"] = None if off else value
        elif param in ("sbloss", "streakbet_loss"):
            changes["streakbet_loss"] = None if off else value
        elif param in ("bbpct", "basebet_pct"):
            changes["basebet_pct"] = None if off else float(value)
        elif param == "multiplier":
            changes["multiplier"] = float(value)
        elif param in ("lossmult", "loss_mult"):
            changes["loss_mult"] = float(value)
        elif param in ("winmult", "win_mult"):
            changes["win_mult"] = float(value)
        elif param == "maxprofit":
            changes["max_profit"] = None if off else float(value)
        elif param == "maxloss":
            changes["max_loss"] = None if off else float(value)
        elif param == "maxbets":
            changes["max_bets"] = None if off else int(value)
        elif param == "maxwins":
            changes["max_wins"] = None if off else int(value)
        elif param == "minbalance":
            changes["stop_on_balance"] = None if off else float(value)
        elif param in ("milestonebets", "msbets"):
            changes["milestone_bets"] = int(value)
        elif param in ("milestonewins", "mswins"):
            changes["milestone_wins"] = int(value)
        elif param in ("profitincrement", "pi"):
            changes["profit_increment"] = None if off else float(value)
        elif param in ("profitthreshold", "pt"):
            changes["profit_threshold"] = None if off else float(value)
        else:
            await update.message.reply_text(f"Unknown tweak param: `{param}`\nUse `/tweak` for options.", parse_mode="Markdown")
            return
    except ValueError:
        await update.message.reply_text(f"Invalid value: `{value}`", parse_mode="Markdown")
        return

    result = await engine.mutate(changes)
    await update.message.reply_text(f"Tweaked slot {slot}: {result}", parse_mode="Markdown")


# ── /pause ───────────────────────────────────────────────
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # /pause all
    if context.args and context.args[0].lower() == "all":
        running = _get_running(user_id)
        for e in running.values():
            e.pause()
        await _reply(update, f"Paused {len(running)} session(s)." if running else "No sessions running.")
        return
    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await _reply(update, err)
        return
    engine.pause()
    await _reply(update, f"Session (slot {slot}) paused. /resume to continue.", parse_mode="Markdown")


# ── /resume ──────────────────────────────────────────────
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # /resume all
    if context.args and context.args[0].lower() == "all":
        running = _get_running(user_id)
        for e in running.values():
            e.resume()
        await _reply(update, f"Resumed {len(running)} session(s)." if running else "No sessions running.")
        return
    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await _reply(update, err)
        return
    engine.resume()
    await _reply(update, f"Session (slot {slot}) resumed.")


# ── /status ──────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    running = _get_running(user_id)

    if not running:
        await update.message.reply_text(
            "No session running. Start one with /bet", parse_mode="Markdown")
        return

    # Multiple sessions and no slot specified — show summary
    if len(running) > 1 and not context.args:
        lines = [f"*{len(running)} Active Sessions:*\n"]
        for slot, engine in sorted(running.items()):
            s = engine.get_status()
            p = s["profit"]
            ps = "+" if p >= 0 else ""
            game = s.get("game", "limbo").capitalize()
            lines.append(
                f"*#{slot}*  {game}  `{s['strategy']}`  `{ps}{p:.8f}`  ({s['bets']} bets)")
        lines.append(f"\nUse `/status <slot>` for detail.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await update.message.reply_text(err)
        return

    text = format_status(engine.get_status())
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Refresh", callback_data=f"refresh_status:{slot}"),
        InlineKeyboardButton("Stop", callback_data=f"stop_session:{slot}"),
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ── /monitor ─────────────────────────────────────────────
async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await update.message.reply_text(err)
        return

    interval = 5
    # Parse interval from args (skip slot number if present)
    for a in (context.args or []):
        try:
            v = int(a)
            if v != slot:
                interval = max(3, min(60, v))
        except ValueError:
            pass

    user_monitors = active_monitors.get(user_id, {})
    old = user_monitors.pop(slot, None)
    if old and not old.done():
        old.cancel()

    text = format_status(engine.get_status())
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Auto {interval}s", callback_data="noop"),
        InlineKeyboardButton("Stop Monitor", callback_data=f"stop_monitor:{slot}"),
        InlineKeyboardButton("Stop Session", callback_data=f"stop_session:{slot}"),
    ]])
    msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    task = asyncio.create_task(
        _monitor_loop(msg, user_id, slot, interval))
    active_monitors.setdefault(user_id, {})[slot] = task


async def _monitor_loop(msg, user_id: int, slot: int, interval: int):
    try:
        while True:
            await asyncio.sleep(interval)
            engine = active_engines.get(user_id, {}).get(slot)
            if not engine or not engine.running:
                # Check if a recurring restart is pending for this slot
                user_rec = recurring_state.get(user_id, {})
                rec = user_rec.get(slot)
                if rec:
                    timer = rec.get("timer_task")
                    if timer and not timer.done():
                        # Keep monitor alive while waiting for recurring restart
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton(f"Auto {interval}s", callback_data="noop"),
                            InlineKeyboardButton("Stop Monitor", callback_data=f"stop_monitor:{slot}"),
                            InlineKeyboardButton("Stop Session", callback_data=f"stop_session:{slot}"),
                        ]])
                        try:
                            await msg.edit_text(
                                "⏳ Session ended. Recurring restart pending…",
                                reply_markup=keyboard)
                        except Exception:
                            pass
                        continue  # keep looping — new engine will appear after restart
                try:
                    await msg.edit_text(
                        "Session ended. Monitor stopped.",
                        reply_markup=None)
                except Exception:
                    pass
                break

            text = format_status(engine.get_status())
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Auto {interval}s", callback_data="noop"),
                InlineKeyboardButton("Stop Monitor", callback_data=f"stop_monitor:{slot}"),
                InlineKeyboardButton("Stop Session", callback_data=f"stop_session:{slot}"),
            ]])
            try:
                await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        user_monitors = active_monitors.get(user_id, {})
        user_monitors.pop(slot, None)
        if not user_monitors:
            active_monitors.pop(user_id, None)


# ── Inline button callbacks ─────────────────────────────
def _parse_callback_slot(data: str) -> tuple:
    """Parse 'action:slot' from callback data. Returns (action, slot)."""
    if ":" in data:
        action, slot_str = data.rsplit(":", 1)
        try:
            return action, int(slot_str)
        except ValueError:
            pass
    return data, None


# ── /web — generate dashboard login link ─────────────────
async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .config import WEB_URL, WEB_SECRET
    if not WEB_URL or not WEB_SECRET:
        await update.message.reply_text(
            "Web dashboard not configured.\n"
            "Set `STAKE_WEB_URL` and `STAKE_WEB_SECRET_KEY` in the bot environment.",
            parse_mode="Markdown")
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or ""

    # Generate a one-time JWT token (5 min expiry)
    try:
        import uuid
        from datetime import datetime, timedelta, timezone
        from jose import jwt as jose_jwt

        expire = datetime.now(timezone.utc) + timedelta(minutes=5)
        jti = str(uuid.uuid4())
        token = jose_jwt.encode(
            {"sub": f"tg:{user_id}", "role": "tg_user", "jti": jti, "username": username, "exp": expire},
            WEB_SECRET,
            algorithm="HS256",
        )
        link = f"{WEB_URL.rstrip('/')}/auth/telegram?token={token}"
        await update.message.reply_text(
            f"🌐 *Web Dashboard*\n\n"
            f"[Open Dashboard]({link})\n\n"
            f"_Link expires in 5 minutes._",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except ImportError:
        await update.message.reply_text(
            "python-jose not installed. Install it in the bot venv:\n"
            "`pip install python-jose[cryptography]`",
            parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_web error: %s", e, exc_info=True)
        msg = f"Error generating link: {e}" if APP_ENV != "production" else "Could not generate web link. Please try again."
        await update.message.reply_text(msg, parse_mode="Markdown")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "noop":
        return

    action, slot = _parse_callback_slot(query.data)

    # Resolve engine — use slot from callback, or auto-resolve single session
    def _get_engine():
        if slot is not None:
            return slot, active_engines.get(user_id, {}).get(slot)
        # Backwards compat: no slot encoded, auto-resolve
        running = _get_running(user_id)
        if len(running) == 1:
            s, e = next(iter(running.items()))
            return s, e
        return None, None

    if action == "refresh_status":
        s, engine = _get_engine()
        if not engine or not engine.running:
            # Keep buttons alive if a recurring restart is pending
            user_rec = recurring_state.get(user_id, {})
            rec = user_rec.get(s) if s is not None else None
            timer = rec.get("timer_task") if rec else None
            if timer and not timer.done():
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Refresh", callback_data=f"refresh_status:{s}"),
                    InlineKeyboardButton("Stop", callback_data=f"stop_session:{s}"),
                ]])
                try:
                    await query.edit_message_text(
                        "⏳ Session ended. Recurring restart pending…",
                        reply_markup=keyboard)
                except Exception:
                    pass
            else:
                try:
                    await query.edit_message_text("Session ended.")
                except Exception:
                    pass
            return
        text = format_status(engine.get_status())
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Refresh", callback_data=f"refresh_status:{s}"),
            InlineKeyboardButton("Stop", callback_data=f"stop_session:{s}"),
        ]])
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            pass

    elif action == "stop_monitor":
        s, engine = _get_engine()
        user_mons = active_monitors.get(user_id, {})
        task = user_mons.pop(s, None) if s else None
        if task and not task.done():
            task.cancel()
        if engine and engine.running:
            text = format_status(engine.get_status())
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Refresh", callback_data=f"refresh_status:{s}"),
                InlineKeyboardButton("Monitor", callback_data=f"start_monitor_5:{s}"),
                InlineKeyboardButton("Stop", callback_data=f"stop_session:{s}"),
            ]])
            try:
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception:
                pass
        else:
            await query.edit_message_text("Monitor stopped. Session ended.")

    elif action.startswith("start_monitor_"):
        interval = int(action.split("_")[-1])
        s, engine = _get_engine()
        if not engine or not engine.running:
            await query.edit_message_text("Session ended.")
            return
        user_mons = active_monitors.get(user_id, {})
        old = user_mons.pop(s, None) if s else None
        if old and not old.done():
            old.cancel()
        text = format_status(engine.get_status())
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Auto {interval}s", callback_data="noop"),
            InlineKeyboardButton("Stop Monitor", callback_data=f"stop_monitor:{s}"),
            InlineKeyboardButton("Stop Session", callback_data=f"stop_session:{s}"),
        ]])
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            pass
        task = asyncio.create_task(
            _monitor_loop(query.message, user_id, s, interval))
        active_monitors.setdefault(user_id, {})[s] = task

    elif action == "stop_session":
        s, engine = _get_engine()
        user_mons = active_monitors.get(user_id, {})
        task = user_mons.pop(s, None) if s else None
        if task and not task.done():
            task.cancel()
        if engine and engine.running:
            engine.stop_reason = "Stopped by user"
            engine.stop()
        await query.edit_message_text("Stopping session…")


# ── /stats ───────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No session history yet.")
        return

    init_db(db_path)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT id, started_at, ended_at,
               UPPER(currency), game, strategy, multiplier, base_bet,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0),
               COALESCE(config_snapshot, '')
        FROM sessions ORDER BY id DESC LIMIT 10
    """).fetchall()

    totals = conn.execute("""
        SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
               COALESCE(SUM(losses),0), COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0),
               COALESCE(MAX(max_win_streak),0), COALESCE(MAX(max_loss_streak),0),
               COALESCE(MAX(profit),0), COALESCE(MIN(profit),0),
               COALESCE(MAX(total_bets),0), COALESCE(AVG(total_bets),0),
               COALESCE(MAX(highest_balance),0), COALESCE(MAX(highest_win),0),
               COALESCE(MAX(biggest_loss),0), COALESCE(AVG(bets_per_minute),0),
               COALESCE(MAX(peak_bps),0), COALESCE(MAX(peak_bpm),0),
               COALESCE(AVG(bets_per_second),0)
        FROM sessions
    """).fetchone()
    conn.close()

    if not rows:
        await update.message.reply_text("No sessions found.")
        return

    lines = ["\U0001f4ca *Session History (last 10)*\n"]
    for row in rows:
        lines.append(format_session_row(row))

    lines.append(format_all_time(totals))
    lines.append(f"\n_View session detail:_ /session <id>")

    text = "\n".join(lines)
    if len(text) <= 4096:
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        mid = text.rfind("\U0001f4ca *All-Time")
        await update.message.reply_text(text[:mid], parse_mode="Markdown")
        await update.message.reply_text(text[mid:], parse_mode="Markdown")


# ── /lastbets ────────────────────────────────────────────
async def cmd_lastbets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    n = int(context.args[0]) if context.args else 10
    n = min(n, 50)

    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No data.")
        return

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT timestamp, game, amount, result_display, state, profit, balance_after
        FROM bets ORDER BY id DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No bets found.")
        return

    await update.message.reply_text(format_lastbets(rows), parse_mode="Markdown")


# ── /rules ───────────────────────────────────────────────
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []

    if not rules:
        await update.message.reply_text(
            "No rules configured.\nAdd with /addrule <json>", parse_mode="Markdown")
        return

    lines = ["*Current Rules:*\n"]
    for i, r in enumerate(rules, 1):
        lines.append(f"`{i}.` {r.description}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /addrule ─────────────────────────────────────────────
async def cmd_addrule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "*Usage:* /addrule <json>\n\n"
            "*Condition types (`cond_type`):*\n"
            "`sequence` — count-based (streaks, every N)\n"
            "`profit` — profit/balance threshold\n"
            "`bet` — bet amount threshold\n\n"
            "*Sequence modes (`cond_mode`):*\n"
            "`every` — Every N total (wins/losses/bets)\n"
            "`every_streak` — Every N in streak (N, 2N, 3N…)\n"
            "`first_streak` — First streak of N (once, resets)\n"
            "`streak_above` — Every bet past streak N\n"
            "`streak_below` — Streak below N\n\n"
            "*Profit/bet modes (`cond_mode`):*\n"
            "`gte` >=  `gt` >  `lte` <=  `lt` <\n\n"
            "*Profit fields (`cond_field`):*\n"
            "`profit` `balance` `wagered`\n\n"
            "*Triggers (`cond_trigger`):*\n"
            "`win` `loss` `bet`\n\n"
            "*Actions (`action`):*\n"
            "_Bet amount:_\n"
            "`reset_amount` — Reset to base bet\n"
            "`increase_amount` — Increase by % (value=1 → +1%)\n"
            "`decrease_amount` — Decrease by %\n"
            "`add_amount` — Add fixed amount\n"
            "`deduct_amount` — Deduct fixed amount\n"
            "`set_amount` — Set exact amount\n"
            "_Win chance:_\n"
            "`reset_winchance` — Reset win chance\n"
            "`set_winchance` — Set win chance\n"
            "`increase_wc` — Increase win chance by %\n"
            "`decrease_wc` — Decrease win chance by %\n"
            "`add_wc` — Add to win chance\n"
            "`deduct_wc` — Deduct from win chance\n"
            "_Payout (multiplier):_\n"
            "`reset_payout` — Reset payout\n"
            "`set_payout` — Set payout\n"
            "`increase_payout` — Increase payout by %\n"
            "`decrease_payout` — Decrease payout by %\n"
            "`add_payout` — Add to payout\n"
            "`deduct_payout` — Deduct from payout\n"
            "_Other:_\n"
            "`switch` — Switch dice above/below\n"
            "`stop` — Stop betting\n"
            "`reset_game` — Full reset\n\n"
            "*Examples:*\n"
            '`/addrule {"cond_type":"sequence","cond_mode":"every","cond_value":1,"cond_trigger":"loss","action":"increase_amount","action_value":1}`\n'
            "_Every loss → increase bet by 1%_\n\n"
            '`/addrule {"cond_type":"sequence","cond_mode":"first_streak","cond_value":5,"cond_trigger":"loss","action":"stop","action_value":0}`\n'
            "_Stop after 5 losses in a row_\n\n"
            '`/addrule {"cond_type":"profit","cond_field":"profit","cond_mode":"gte","cond_value":0.01,"action":"stop","action_value":0}`\n'
            "_Stop when profit >= 0.01_",
            parse_mode="Markdown",
        )
        return

    raw = " ".join(context.args)
    # Telegram replaces " with smart quotes — fix them
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        d = json.loads(raw)
        r = StrategyRule.from_dict(d)
        r.description = describe_rule(r)
    except Exception as e:
        logger.error("cmd_addrule error: %s", e, exc_info=True)
        msg = f"Invalid JSON: `{e}`" if APP_ENV != "production" else "Invalid rule format. Please check the syntax."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []
    rules.append(r)

    config["custom_rules_text"] = json.dumps([rl.to_dict() for rl in rules])
    save_user_config(user_id, config)
    await update.message.reply_text(f"Rule added: `{r.description}`", parse_mode="Markdown")


# ── /delrule ────────────────────────────────────────────
async def cmd_delrule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /delrule <number>\nSee /rules for rule numbers.")
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid number. Usage: /delrule <number>")
        return

    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []

    if idx < 1 or idx > len(rules):
        await update.message.reply_text(f"Invalid rule number. You have {len(rules)} rule(s).")
        return

    removed = rules.pop(idx - 1)
    config["custom_rules_text"] = json.dumps([rl.to_dict() for rl in rules]) if rules else ""
    save_user_config(user_id, config)
    await update.message.reply_text(f"Deleted rule {idx}: `{removed.description}`", parse_mode="Markdown")


# ── /editrule ───────────────────────────────────────────
async def cmd_editrule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /editrule <number> <json>\n"
            "Example: /editrule 2 {\"action_value\":200}\n\n"
            "Editable fields: `cond_type`, `cond_mode`, `cond_value`, "
            "`cond_trigger`, `cond_field`, `action`, `action_value`",
            parse_mode="Markdown",
        )
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("First argument must be a rule number.")
        return

    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []

    if idx < 1 or idx > len(rules):
        await update.message.reply_text(f"Invalid rule number. You have {len(rules)} rule(s).")
        return

    raw = " ".join(context.args[1:])
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        patch = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("cmd_editrule JSON error: %s", e, exc_info=True)
        msg = f"Invalid JSON: `{e}`" if APP_ENV != "production" else "Invalid rule format. Please check the syntax."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # Merge patch into existing rule
    old_dict = rules[idx - 1].to_dict()
    old_dict.update(patch)
    try:
        new_rule = StrategyRule.from_dict(old_dict)
        new_rule.description = describe_rule(new_rule)
    except Exception as e:
        logger.error("cmd_editrule error: %s", e, exc_info=True)
        msg = f"Invalid rule after edit: `{e}`" if APP_ENV != "production" else "Invalid rule. Please check the syntax."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    rules[idx - 1] = new_rule
    config["custom_rules_text"] = json.dumps([rl.to_dict() for rl in rules])
    save_user_config(user_id, config)
    await update.message.reply_text(f"Updated rule {idx}: `{new_rule.description}`", parse_mode="Markdown")


# ── /clearrules ──────────────────────────────────────────
async def cmd_clearrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    config["custom_rules_text"] = ""
    save_user_config(user_id, config)
    await update.message.reply_text("All rules cleared.")


# ── /presets ─────────────────────────────────────────────
async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    presets = load_presets(user_id)
    if not presets:
        await update.message.reply_text("No presets saved.")
        return

    lines = ["*Presets:*\n"]
    for name, p in presets.items():
        game = p.get("game", "limbo")
        lines.append(f"`{name}` — {p.get('strategy', '?')} {game} {p.get('multiplier_target', '?')}x")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /savepreset ──────────────────────────────────────────
async def cmd_savepreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /savepreset <name>", parse_mode="Markdown")
        return
    name = context.args[0]
    config = load_user_config(user_id)

    presets = load_presets(user_id)
    presets[name] = {k: config.get(k) for k in CONFIG_KEYS if k not in ("access_token", "lockdown_token", "cookie")}
    save_presets(user_id, presets)
    await update.message.reply_text(f"Preset `{name}` saved.", parse_mode="Markdown")


# ── /loadpreset ──────────────────────────────────────────
async def cmd_loadpreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /loadpreset <name>", parse_mode="Markdown")
        return
    name = context.args[0]

    presets = load_presets(user_id)
    if name not in presets:
        await update.message.reply_text(
            f"Preset `{name}` not found.", parse_mode="Markdown")
        return

    config = load_user_config(user_id)
    for k, v in presets[name].items():
        config[k] = v
    save_user_config(user_id, config)
    await update.message.reply_text(
        f"Preset `{name}` loaded. Check with /config", parse_mode="Markdown")


# ── /session <id> ───────────────────────────────────────
async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /session <id>\nSee /stats for session IDs.",
            parse_mode="Markdown")
        return

    try:
        session_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Session ID must be a number.")
        return

    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No data.")
        return

    init_db(db_path)
    conn = sqlite3.connect(db_path)
    sess = conn.execute("""
        SELECT id, started_at, ended_at,
               UPPER(currency), game, strategy, multiplier, base_bet,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0),
               COALESCE(config_snapshot, '')
        FROM sessions WHERE id = ?
    """, (session_id,)).fetchone()

    if not sess:
        conn.close()
        await update.message.reply_text(f"Session #{session_id} not found.")
        return

    # streak distribution
    bet_rows = conn.execute(
        "SELECT state FROM bets WHERE session_id=? ORDER BY id",
        (session_id,)
    ).fetchall()

    dist = {"win": {}, "loss": {}}
    cur_type = None
    cur_len = 0
    for (st,) in bet_rows:
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

    # recent bets (includes game + result_display)
    recent = conn.execute("""
        SELECT timestamp, amount, result_value, result_display, state, profit, balance_after
        FROM bets WHERE session_id = ? ORDER BY id DESC LIMIT 20
    """, (session_id,)).fetchall()
    conn.close()

    text = format_session_detail(sess, dist, recent)
    if len(text) <= 4096:
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        marker = "\U0001f4dd *Last"
        idx = text.find(marker)
        if idx > 0:
            await update.message.reply_text(text[:idx], parse_mode="Markdown")
            await update.message.reply_text(text[idx:], parse_mode="Markdown")
        else:
            await update.message.reply_text(text[:4096], parse_mode="Markdown")


# ── GRACEFUL SHUTDOWN / RESUME ─────────────────────────
RESUME_FILE = os.path.join(DATA_DIR, "_resume.json")


def save_resume_state():
    """Called on graceful shutdown. Pause all engines, flush DB, save state."""
    if not active_engines:
        if os.path.exists(RESUME_FILE):
            os.remove(RESUME_FILE)
        return

    # Pause all engines immediately
    for engines_dict in active_engines.values():
        for engine in engines_dict.values():
            engine.paused = True
    time.sleep(1)  # let in-flight bets finish

    snapshots = []
    for user_id, engines_dict in list(active_engines.items()):
        for slot, engine in list(engines_dict.items()):
            try:
                engine._flush_bets()
                engine._db_save_session()
                snap = engine.snapshot_state()
                snap["chat_id"] = _engine_chat_ids.get(user_id)
                snap["slot"] = slot
                snapshots.append(snap)
                engine.running = False
                engine._close_conn()
            except Exception as e:
                logger.error("Failed to snapshot user %d slot %d: %s", user_id, slot, e)

    if snapshots:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(RESUME_FILE, "w") as f:
            json.dump(snapshots, f)
        os.chmod(RESUME_FILE, 0o600)
        logger.info("Saved %d engine(s) for resume", len(snapshots))


# ── /cleanup ──────────────────────────────────────────────
async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No session data yet.")
        return

    days = 3
    if context.args:
        try:
            days = max(0, int(context.args[0]))
        except ValueError:
            await update.message.reply_text("Usage: /cleanup [days]\nDefault: 3")
            return

    await update.message.reply_text(f"Cleaning up bet records older than {days} day(s)…")
    deleted = cleanup_old_bets(db_path, bet_age_days=days)
    await update.message.reply_text(
        f"Deleted *{deleted:,}* bet record(s).\n"
        f"Session statistics are preserved.",
        parse_mode="Markdown",
    )


# ── /delsession ──────────────────────────────────────────
async def cmd_delsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No session data yet.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /delsession <session\\_id>")
        return

    try:
        session_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Session ID must be a number.")
        return

    # Block deletion of currently running sessions
    running = _get_running(user_id)
    for engine in running.values():
        if engine.session_id == session_id:
            await update.message.reply_text(
                f"Session #{session_id} is currently running. Stop it first with /stop.")
            return

    conn = sqlite3.connect(db_path, timeout=10)
    row = conn.execute("SELECT id, total_bets, profit FROM sessions WHERE id = ?",
                       (session_id,)).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text(f"Session #{session_id} not found.")
        return

    conn.execute("DELETE FROM bets WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    await update.message.reply_text(
        f"Deleted session *#{session_id}* ({row[1]:,} bets, profit: {row[2]:.8f}).",
        parse_mode="Markdown",
    )


async def load_resume_state(application):
    """Called on startup. Restore engines from resume file."""
    if not os.path.exists(RESUME_FILE):
        return

    try:
        with open(RESUME_FILE) as f:
            snapshots = json.load(f)
    except Exception as e:
        logger.error("Failed to load resume file: %s", e)
        return
    finally:
        os.remove(RESUME_FILE)  # consume it — don't re-resume on crash

    resumed = 0

    for snap in snapshots:
        user_id = snap["user_id"]
        chat_id = snap.get("chat_id")
        config = snap["config"]
        db_path = snap["db_path"]
        slot = snap.get("slot", _alloc_slot(user_id))

        if not chat_id:
            logger.warning("No chat_id for user %d, skipping resume", user_id)
            continue

        try:
            engine = BettingEngine(user_id, db_path, config)
            engine.restore_state(snap)

            def _make_on_stop(cid, uid, s):
                def on_stop(reason):
                    t = asyncio.create_task(_notify_stop(cid, uid, s, reason, application))
                    t.add_done_callback(_log_task_exception)
                return on_stop

            def _make_on_milestone(cid):
                def on_milestone(data):
                    t = asyncio.create_task(_notify_milestone(cid, data, application))
                    t.add_done_callback(_log_task_exception)
                return on_milestone

            def _make_on_error(cid):
                def on_error(msg):
                    t = asyncio.create_task(_notify_error(cid, msg, application))
                    t.add_done_callback(_log_task_exception)
                return on_error

            engine.on_stop = _make_on_stop(chat_id, user_id, slot)
            engine.on_milestone = _make_on_milestone(chat_id)
            engine.on_error = _make_on_error(chat_id)

            started = False
            for attempt in range(3):
                if await engine.start_resumed():
                    started = True
                    break
                logger.warning("Resume attempt %d/3 failed for user %d slot %d: %s",
                               attempt + 1, user_id, slot, engine.last_error)
                await asyncio.sleep(5)

            if started:
                active_engines.setdefault(user_id, {})[slot] = engine
                _engine_chat_ids[user_id] = chat_id
                resumed += 1
                await application.bot.send_message(
                    chat_id,
                    f"Bot updated — session #{engine.session_id} (slot {slot}) resumed.\n"
                    f"Use /status to check progress.",
                )
            else:
                logger.error("Resume failed for user %d slot %d: %s", user_id, slot, engine.last_error)
                await application.bot.send_message(
                    chat_id,
                    f"Resume failed for session #{snap.get('session_id', '?')} (slot {slot}):\n"
                    + (f"`{engine.last_error}`\n" if APP_ENV != "production" else "")
                    + "Use /bet to start a new session.",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error("Resume failed for user %d: %s", user_id, e)

    if resumed:
        logger.info("Resumed %d/%d engines", resumed, len(snapshots))
