"""Read-only access to per-user bot SQLite databases — delegates to shared library."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from .config import settings
from shared.web.bot_db import (  # noqa: E402
    discover_users as _discover_users,
    get_user_config as _get_user_config,
    save_user_config as _save_user_config,
    get_sessions as _get_sessions,
    get_session_count as _get_session_count,
    get_session as _get_session,
    get_session_stats as _get_session_stats,
    get_bets as _get_bets,
    get_bet_count as _get_bet_count,
    get_bets_for_chart as _get_bets_for_chart,
    get_platform_stats as _get_platform_stats,
)

DB_FILENAME = "stake.db"
_CHART_COLS = "id, timestamp, amount, multiplier_target, result_value, result_display, state, profit, balance_after"
_TARGET_COL = "multiplier_target"
_RESULT_COL = "result_display"

discover_users = lambda: _discover_users(settings.bot_data_dir, DB_FILENAME)
get_user_config = lambda user_id: _get_user_config(settings.bot_data_dir, user_id)
save_user_config = lambda user_id, config: _save_user_config(settings.bot_data_dir, user_id, config)
get_sessions = lambda user_id, limit=50, offset=0: _get_sessions(settings.bot_data_dir, DB_FILENAME, user_id, limit, offset)
get_session_count = lambda user_id: _get_session_count(settings.bot_data_dir, DB_FILENAME, user_id)
get_session = lambda user_id, session_id: _get_session(settings.bot_data_dir, DB_FILENAME, user_id, session_id)
get_session_stats = lambda user_id: _get_session_stats(settings.bot_data_dir, DB_FILENAME, user_id)
get_bets = lambda user_id, session_id, limit=100, offset=0: _get_bets(settings.bot_data_dir, DB_FILENAME, user_id, session_id, limit, offset)
get_bet_count = lambda user_id, session_id: _get_bet_count(settings.bot_data_dir, DB_FILENAME, user_id, session_id)
get_bets_for_chart = lambda user_id, session_id, max_points=2000: _get_bets_for_chart(settings.bot_data_dir, DB_FILENAME, _CHART_COLS, _TARGET_COL, _RESULT_COL, user_id, session_id, max_points)
get_platform_stats = lambda: _get_platform_stats(settings.bot_data_dir, DB_FILENAME)
