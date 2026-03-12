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


def _update_dice_target(mutations: dict, dice_condition: str, wc: float, new_mult: float):
    """Helper: set multiplier_target and dice_target from win chance."""
    mutations["multiplier_target"] = new_mult
    if dice_condition == "above":
        mutations["dice_target"] = round(100.0 - wc, 2)
    else:
        mutations["dice_target"] = round(wc, 2)


def apply_action(rule: StrategyRule, current_bet: float, base_bet: float,
                 dice_condition: str, multiplier: float, current_streak: int,
                 initial_multiplier: float = 0.0
                 ) -> dict:
    """Apply a rule action and return a dict of state mutations.

    For Stake: dice uses condition (above/below) and target instead of
    wolfbet's rule (over/under) and bet_value.

    Percentage convention (matches Stake autobet):
      value=1 → multiply by 1.01 → +1%
      value=50 → multiply by 1.50 → +50%
    """
    a = rule.action
    v = rule.action_value
    mutations = {}
    wc = 99.0 / multiplier if multiplier > 0 else 49.5
    init_mult = initial_multiplier or multiplier

    # ── Bet amount actions ──
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

    # ── Dice switch ──
    elif a == "switch":
        new_cond = "below" if dice_condition == "above" else "above"
        mutations["dice_condition"] = new_cond
        if new_cond == "above":
            mutations["dice_target"] = round(100.0 - wc, 2)
        else:
            mutations["dice_target"] = round(wc, 2)

    # ── Stop ──
    elif a == "stop":
        mutations["running"] = False
        mutations["stop_reason"] = f"Rule stop: {rule.description}"

    # ── Win chance actions ──
    elif a == "reset_winchance":
        init_wc = 99.0 / init_mult if init_mult > 0 else 49.5
        _update_dice_target(mutations, dice_condition, init_wc, init_mult)
    elif a == "set_winchance":
        new_wc = max(0.01, min(98.99, v))
        new_mult = round(99.0 / new_wc, 4)
        _update_dice_target(mutations, dice_condition, new_wc, new_mult)
    elif a == "increase_wc":
        new_wc = min(98.99, wc * (1 + v / 100))
        new_mult = round(99.0 / new_wc, 4)
        _update_dice_target(mutations, dice_condition, new_wc, new_mult)
    elif a == "decrease_wc":
        new_wc = max(0.01, wc * (1 - v / 100))
        new_mult = round(99.0 / new_wc, 4)
        _update_dice_target(mutations, dice_condition, new_wc, new_mult)
    elif a == "add_wc":
        new_wc = max(0.01, min(98.99, wc + v))
        new_mult = round(99.0 / new_wc, 4)
        _update_dice_target(mutations, dice_condition, new_wc, new_mult)
    elif a == "deduct_wc":
        new_wc = max(0.01, min(98.99, wc - v))
        new_mult = round(99.0 / new_wc, 4)
        _update_dice_target(mutations, dice_condition, new_wc, new_mult)

    # ── Payout (multiplier) actions ──
    elif a == "reset_payout":
        init_wc = 99.0 / init_mult if init_mult > 0 else 49.5
        _update_dice_target(mutations, dice_condition, init_wc, init_mult)
    elif a == "set_payout":
        new_mult = max(1.01, v)
        new_wc = 99.0 / new_mult
        _update_dice_target(mutations, dice_condition, new_wc, round(new_mult, 4))
    elif a == "increase_payout":
        new_mult = multiplier * (1 + v / 100)
        new_wc = max(0.01, 99.0 / new_mult)
        _update_dice_target(mutations, dice_condition, new_wc, round(new_mult, 4))
    elif a == "decrease_payout":
        new_mult = max(1.01, multiplier * (1 - v / 100))
        new_wc = 99.0 / new_mult
        _update_dice_target(mutations, dice_condition, new_wc, round(new_mult, 4))
    elif a == "add_payout":
        new_mult = multiplier + v
        new_wc = max(0.01, 99.0 / new_mult)
        _update_dice_target(mutations, dice_condition, new_wc, round(new_mult, 4))
    elif a == "deduct_payout":
        new_mult = max(1.01, multiplier - v)
        new_wc = 99.0 / new_mult
        _update_dice_target(mutations, dice_condition, new_wc, round(new_mult, 4))

    # ── Full reset ──
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
