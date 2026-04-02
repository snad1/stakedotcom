#!/usr/bin/env python3
"""Stake Telegram Bot — entry point and Application wiring."""

import atexit
import os
import signal
import sys

from telegram import BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from .config import BOT_TOKEN, DATA_DIR, logger
from . import VERSION
from .handlers import (
    cmd_start, cmd_help, cmd_settoken, cmd_benchmark, cmd_balance, cmd_config,
    cmd_set, cmd_strategies, cmd_bet, cmd_stop, cmd_tweak, cmd_pause, cmd_resume,
    cmd_status, cmd_monitor, cmd_stats, cmd_lastbets,
    cmd_rules, cmd_addrule, cmd_delrule, cmd_editrule, cmd_clearrules,
    cmd_presets, cmd_savepreset, cmd_loadpreset,
    cmd_session, cmd_web,
    cmd_cleanup, cmd_delsession,
    callback_handler,
    save_resume_state, load_resume_state,
)

_resume_saved = False

def _save_once():
    """Ensure resume state is saved exactly once on shutdown."""
    global _resume_saved
    if _resume_saved:
        return
    _resume_saved = True
    logger.info("Saving resume state…")
    save_resume_state()

def _signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT — save resume state before exit."""
    logger.info("Received signal %d, saving state…", signum)
    _save_once()
    sys.exit(0)


def main():
    if not BOT_TOKEN:
        print("Error: Set STAKE_TG_TOKEN environment variable")
        print("  export STAKE_TG_TOKEN='your_telegram_bot_token'")
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    atexit.register(_save_once)
    logger.info("Stake Telegram Bot v%s starting…", VERSION)
    logger.info("Data dir: %s", DATA_DIR)

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",       "Show welcome and quick start guide"),
            BotCommand("help",        "Full command reference"),
            BotCommand("settoken",    "Set Stake access tokens"),
            BotCommand("balance",     "Check balances"),
            BotCommand("config",      "View current config"),
            BotCommand("set",         "Set a parameter"),
            BotCommand("strategies",  "List strategies"),
            BotCommand("bet",         "Start betting session"),
            BotCommand("stop",        "Stop session"),
            BotCommand("tweak",       "Change settings on a running session"),
            BotCommand("pause",       "Pause betting"),
            BotCommand("resume",      "Resume betting"),
            BotCommand("status",      "Live status with refresh button"),
            BotCommand("monitor",     "Auto-updating live status"),
            BotCommand("stats",       "Session history"),
            BotCommand("lastbets",    "Recent bets"),
            BotCommand("session",     "Detailed session report"),
            BotCommand("rules",       "List current rules"),
            BotCommand("addrule",     "Add a rule (JSON)"),
            BotCommand("delrule",     "Delete a rule by number"),
            BotCommand("editrule",    "Edit a rule by number"),
            BotCommand("clearrules",  "Clear all rules"),
            BotCommand("presets",     "List presets"),
            BotCommand("savepreset",  "Save current config as preset"),
            BotCommand("loadpreset",  "Load a preset"),
            BotCommand("web",         "Open web dashboard"),
            BotCommand("benchmark",   "Test API response speed"),
            BotCommand("cleanup",     "Purge old bet records"),
            BotCommand("delsession",  "Delete a session by ID"),
        ])
        logger.info("Bot commands registered with Telegram")
        # Resume any engines saved from graceful shutdown
        await load_resume_state(application)

    async def post_shutdown(application):
        _save_once()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settoken", cmd_settoken))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("strategies", cmd_strategies))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("tweak", cmd_tweak))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("lastbets", cmd_lastbets))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("addrule", cmd_addrule))
    app.add_handler(CommandHandler("delrule", cmd_delrule))
    app.add_handler(CommandHandler("editrule", cmd_editrule))
    app.add_handler(CommandHandler("clearrules", cmd_clearrules))
    app.add_handler(CommandHandler("presets", cmd_presets))
    app.add_handler(CommandHandler("savepreset", cmd_savepreset))
    app.add_handler(CommandHandler("loadpreset", cmd_loadpreset))
    app.add_handler(CommandHandler("web", cmd_web))
    app.add_handler(CommandHandler("benchmark", cmd_benchmark))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("delsession", cmd_delsession))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot started. Polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
