"""Strategy definitions, rule engine, and rule parsing.

Single source of truth for strategies and rules.
Both the CLI bot and Telegram bot import from here.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared.core.strategy import (  # noqa: E402
    make_lookups,
    RULE_ACTIONS, RULE_ACTION_LABELS,
    SEQ_MODES,
    StrategyRule,
    describe_rule, cmp, load_rules_from_text,
)

STRATEGIES = {
    "1": ("Flat Bet",          "Same amount every bet"),
    "2": ("Martingale",        "Double on loss, reset on win"),
    "3": ("Anti-Martingale",   "Double on win, reset on loss"),
    "4": ("D'Alembert",        "Add 1 unit on loss, remove on win"),
    "5": ("Paroli (3-step)",   "Double on win up to 3x then reset"),
    "6": ("Delay Martingale",  "Flat for N losses then double"),
    "7": ("Rule-Based",        "Custom conditions & actions"),
}

STRATEGY_NAMES, STRATEGY_BY_NAME = make_lookups(STRATEGIES)
