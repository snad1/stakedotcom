"""Shared engine logic — strategy computation, rule evaluation, action application.

Pure functions that take explicit parameters. No global state.
Used by both CLI bot (stake.py) and Telegram bot (tg/engine.py).

Key differences from wolfbet core/engine:
  - No WolfRider strategy (Stake has 7 strategies, keys 1-7)
  - Dice uses condition/target instead of rule/bet_value
  - Switch action toggles dice condition (above/below)
  - Win chance calculations: dice target = 99/multiplier (below) or 100-99/multiplier (above)
"""

from .strategy import StrategyRule, cmp


def compute_next_bet(strategy_key: str, base_bet: float, current_bet: float,
                     loss_mult: float, win_mult: float, last_result: str,
                     current_streak: int, delay_threshold: int = 3,
                     dalembert_unit: int = 0, paroli_count: int = 0
                     ) -> tuple:
    """Compute the next bet amount based on strategy.

    Returns (next_bet, dalembert_unit, paroli_count) — callers must persist
    the dalembert_unit and paroli_count for stateful strategies.
    """
    key = strategy_key

    if key == "1":  # Flat
        return base_bet, dalembert_unit, paroli_count
    elif key == "2":  # Martingale
        nxt = (current_bet * loss_mult) if last_result == "loss" else base_bet
        return nxt, dalembert_unit, paroli_count
    elif key == "3":  # Anti-Martingale
        nxt = (current_bet * win_mult) if last_result == "win" else base_bet
        return nxt, dalembert_unit, paroli_count
    elif key == "4":  # D'Alembert
        if last_result == "loss":
            dalembert_unit += 1
        elif last_result == "win" and dalembert_unit > 0:
            dalembert_unit -= 1
        return base_bet * (1 + dalembert_unit), dalembert_unit, paroli_count
    elif key == "5":  # Paroli
        if last_result == "win":
            paroli_count += 1
            if paroli_count >= 3:
                paroli_count = 0
                return base_bet, dalembert_unit, paroli_count
            return current_bet * win_mult, dalembert_unit, paroli_count
        else:
            paroli_count = 0
            return base_bet, dalembert_unit, paroli_count
    elif key == "6":  # Delay Martingale
        if last_result == "win":
            return base_bet, dalembert_unit, paroli_count
        consec = abs(min(current_streak, 0))
        nxt = base_bet if consec <= delay_threshold else current_bet * loss_mult
        return nxt, dalembert_unit, paroli_count
    elif key == "7":  # Rule-Based
        return current_bet, dalembert_unit, paroli_count

    return base_bet, dalembert_unit, paroli_count


def apply_action(rule: StrategyRule, current_bet: float, base_bet: float,
                 dice_condition: str, multiplier: float, current_streak: int
                 ) -> dict:
    """Apply a rule action and return a dict of state mutations.

    For Stake: dice uses condition (above/below) and target instead of
    wolfbet's rule (over/under) and bet_value.
    """
    a = rule.action
    v = rule.action_value
    mutations = {}

    if a == "reset_amount":
        mutations["current_bet"] = base_bet
    elif a == "increase_amount":
        mutations["current_bet"] = current_bet * (1 + v / 100)
    elif a == "decrease_amount":
        mutations["current_bet"] = max(base_bet, current_bet * (1 - v / 100))
    elif a == "add_amount":
        mutations["current_bet"] = current_bet + v
    elif a == "deduct_amount":
        mutations["current_bet"] = max(base_bet, current_bet - v)
    elif a == "set_amount":
        mutations["current_bet"] = max(base_bet, v)
    elif a == "switch":
        # Toggle dice condition above ↔ below
        new_cond = "below" if dice_condition == "above" else "above"
        wc = 99.0 / multiplier
        mutations["dice_condition"] = new_cond
        if new_cond == "above":
            mutations["dice_target"] = round(100.0 - wc, 2)
        else:
            mutations["dice_target"] = round(wc, 2)
    elif a == "stop":
        mutations["running"] = False
        mutations["stop_reason"] = f"Rule stop: {rule.description}"
    elif a == "set_winchance":
        wc = max(0.01, min(98.99, v))
        new_mult = round(99.0 / wc, 4)
        mutations["multiplier_target"] = new_mult
        # Update dice target for current condition
        if dice_condition == "above":
            mutations["dice_target"] = round(100.0 - wc, 2)
        else:
            mutations["dice_target"] = round(wc, 2)
    elif a == "increase_wc":
        wc = min(98.99, (99.0 / multiplier) * (1 + v / 100))
        new_mult = round(99.0 / wc, 4)
        mutations["multiplier_target"] = new_mult
        if dice_condition == "above":
            mutations["dice_target"] = round(100.0 - wc, 2)
        else:
            mutations["dice_target"] = round(wc, 2)
    elif a == "decrease_wc":
        wc = max(0.01, (99.0 / multiplier) * (1 - v / 100))
        new_mult = round(99.0 / wc, 4)
        mutations["multiplier_target"] = new_mult
        if dice_condition == "above":
            mutations["dice_target"] = round(100.0 - wc, 2)
        else:
            mutations["dice_target"] = round(wc, 2)
    elif a == "reset_game":
        mutations["current_bet"] = base_bet
        mutations["current_streak"] = 0

    return mutations


def evaluate_rules(rules, bet_state: str, wins: int, losses: int,
                   total_bets: int, current_streak: int, profit: float,
                   current_balance: float, current_bet: float,
                   multiplier: float) -> list:
    """Evaluate all rules against current state. Returns list of triggered StrategyRule objects."""
    triggered = []
    for rule in rules:
        hit = False
        if rule.cond_type == "sequence":
            streak = current_streak
            trig = rule.cond_trigger
            n = rule.cond_value
            if rule.cond_mode == "every":
                if trig == "win" and bet_state == "win":
                    hit = (wins > 0 and wins % int(n) == 0)
                elif trig == "loss" and bet_state == "loss":
                    hit = (losses > 0 and losses % int(n) == 0)
                elif trig == "bet":
                    hit = (total_bets > 0 and total_bets % int(n) == 0)
            elif rule.cond_mode == "every_streak":
                if trig == "win" and bet_state == "win":
                    hit = (streak > 0 and streak % int(n) == 0)
                elif trig == "loss" and bet_state == "loss":
                    hit = (abs(streak) > 0 and abs(streak) % int(n) == 0)
            elif rule.cond_mode == "first_streak":
                if trig == "win" and bet_state == "win":
                    hit = (streak == int(n))
                elif trig == "loss" and bet_state == "loss":
                    hit = (abs(streak) == int(n))
            elif rule.cond_mode == "streak_above":
                if trig == "win" and bet_state == "win":
                    hit = (streak > int(n))
                elif trig == "loss" and bet_state == "loss":
                    hit = (abs(streak) > int(n))
            elif rule.cond_mode == "streak_below":
                if trig == "win" and bet_state == "win":
                    hit = (streak < int(n))
                elif trig == "loss" and bet_state == "loss":
                    hit = (abs(streak) < int(n))
        elif rule.cond_type == "profit":
            f = rule.cond_field
            if f == "profit" and profit > 0:
                hit = cmp(profit, rule.cond_mode, rule.cond_value)
            elif f == "loss" and profit < 0:
                hit = cmp(abs(profit), rule.cond_mode, rule.cond_value)
            elif f == "balance":
                hit = cmp(current_balance, rule.cond_mode, rule.cond_value)
        elif rule.cond_type == "bet":
            f = rule.cond_field
            if f == "amount":     hit = cmp(current_bet, rule.cond_mode, rule.cond_value)
            elif f == "number":   hit = cmp(total_bets, rule.cond_mode, rule.cond_value)
            elif f == "winchance": hit = cmp(99.0 / multiplier, rule.cond_mode, rule.cond_value)
            elif f == "payout":   hit = cmp(multiplier, rule.cond_mode, rule.cond_value)
        if hit:
            triggered.append(rule)
    return triggered
