# Stake AutoBot v1.2

High-speed multi-game auto-betting engine for Stake.com with a live, ultra-compact terminal dashboard. Supports Limbo and Dice out of the box — extensible game registry makes adding new games trivial. Runs on any VPS or local machine — leave it running in tmux and check stats any time.

---

## Features

| Feature | Details |
|---|---|
| **Pure ANSI TUI** | Direct ANSI rendering (no Rich flicker), fits any terminal, refreshes 2x/sec |
| **Multi-game support** | Limbo + Dice via pluggable game registry — add new games in ~20 lines |
| **7 betting strategies** | Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, Delay Martingale, Rule-Based |
| **Rule-based engine** | Build custom IF/THEN rules with interactive wizard — sequence, profit, and bet conditions |
| **Editable multipliers** | Customize loss/win multipliers per strategy (no more hardcoded 2x) |
| **Rule editor** | Edit any part of existing rules, delete, add, clear, or rebuild from scratch |
| **Named presets** | Save & load strategy configs by name with `--preset` |
| **Smart rate recovery** | Exponential probe instead of hard freeze — resumes as soon as capacity restores |
| **Smart stop conditions** | Max profit, max loss, max bets, max wins, min balance floor |
| **Safety cap** | Never bets more than 20% of current balance in one go |
| **Session database** | SQLite log of every bet and session — survives restarts |
| **Config persistence** | Save/load config with `--resume`, smart wizard with defaults |
| **Monitor mode** | `--monitor` to attach a live TUI to a running daemon — pause/resume/stop remotely |
| **Daemon mode** | `--daemon` for background running, `--status` to check, `--stop` to halt |
| **Performance tracking** | BPS, BPM, peak/low speed ranges, peak/low balance, best win, worst loss, avg P/L |
| **Streak distribution** | Per-session bar charts showing win/loss streak frequency |
| **Test mode** | Set bet amount to `0` for free bets (no real money risked) |
| **Cloudflare bypass** | Full cookie passthrough for Cloudflare-protected endpoints |
| **Daily log rotation** | Logs at `~/.stake_logs/stake.log`, rotates at midnight, 30-day retention |
| **Interactive keyboard** | `P` pause, `R` resume, `H` history, `Q` quit — all from dashboard |

---

## Quick Start

### Mac / Local

```bash
# 1. Setup
cd stake
python3 -m venv .venv && source .venv/bin/activate
pip install requests rich

# 2. Run (wizard guides you through config)
python3 stake.py

# 3. You'll need from your browser DevTools (F12 → Network tab → any stake.com request):
#    - x-access-token (from request headers)
#    - x-lockdown-token (from request headers)
#    - Full Cookie string (for Cloudflare bypass)

# 4. Resume with saved config
python3 stake.py --resume
```

### VPS

```bash
# Upload and install
scp stake.py requirements.txt user@your-vps:~/
ssh user@your-vps
python3 -m venv .venv && source .venv/bin/activate
pip install requests rich

# Run in tmux (survives SSH disconnect)
tmux new -s stake
python3 stake.py
# Detach: Ctrl+B then D
# Re-attach: tmux attach -t stake
```

---

## Dashboard

The dashboard uses pure ANSI rendering (no Rich flicker) — pinned rows + adaptive recent bets. Fits any terminal.

```text
─── Stake v1.1  Limbo  #3  00:12:34  ● LIVE ──────────────────────
 BAL  0.00845321 USDT  PnL +0.00012500  WAG 0.00832100  WR 51.2%
 Bets 2042  W 1046  L 996  Str W+4  Best W+11/L-8
 Bet 0.00010000  Hi 0.00640000  Martingale  2.0x  BPS 5.2  BPM 312
 Peak 0.00860000  Low 0.00800000  BestW +0.00009800  WorstL -0.00640000  Avg +0.00000006
 P/L ▃▅▇▆▄▅▇█▆▅▃▄▆▇
──────────────────────────────────────────────────────────────────
     #  Time      Amount          Result  W/L   P/L              Balance
  2042  14:22:01  0.00010000     2.31x    W   +0.00009800     0.00845321
  2041  14:22:00  0.00010000     1.02x    L   -0.00010000     0.00835521
──────────────────────────────────────────────────────────────────
 W WIN | Result: 2.31x | P/L: +0.00009800      [P]ause [R]esume [Q]uit [H]istory
```

---

## Games

| Game | How it works | Key field |
|---|---|---|
| **Limbo** | Pick a target multiplier — if the roll exceeds it, you win | `multiplierTarget` (e.g. 2.0 = 2x payout) |
| **Dice** | Pick a target number + above/below — if the roll matches, you win | `target` + `condition` (e.g. 50.5 above = 2x payout) |

### Dice target math

- **Above**: `target = 100 - 99/multiplier` (e.g. 2x → 50.5)
- **Below**: `target = 99/multiplier` (e.g. 2x → 49.5)

### Adding new games

Games are registered via the `_register_game()` function. To add a new game, define a `build_payload` and `parse_result` function, then register it — ~20 lines of code.

---

## Strategies

| # | Name | How it works | Risk |
|---|---|---|---|
| 1 | **Flat Bet** | Same amount every time | Very Low |
| 2 | **Martingale** | Multiply on loss, reset on win (configurable multiplier) | High |
| 3 | **Anti-Martingale** | Multiply on win, reset on loss (configurable multiplier) | Medium |
| 4 | **D'Alembert** | +1 unit on loss, -1 on win | Low-Medium |
| 5 | **Paroli (3-step)** | Multiply on win up to 3x then reset (configurable multiplier) | Medium |
| 6 | **Delay Martingale** | Flat for N consecutive losses, then start multiplying | High |
| 7 | **Rule-Based** | Build custom IF/THEN conditions & actions (see below) | Variable |

> Strategies 2, 3, 5, 6 let you customize the loss/win multiplier in the wizard (e.g. 2.01x, 1.5x, 3x).
>
> The built-in 20% balance safety cap limits runaway bets, but always set a **Max Loss** stop condition.

---

## Rule-Based Strategy (Strategy 7)

Build custom rules interactively using the wizard. Each rule is an **IF condition → THEN action** pair.

### Conditions

| Type | Options | Description |
|---|---|---|
| **Sequence** | Every N / Every streak of N / Streak above N / Streak below N | Trigger on win/loss/bet counts or streaks |
| **Profit** | On profit / loss / balance | Compare with `>=`, `>`, `<=`, `<` against a value |
| **Bet** | On bet amount / number / win chance / payout | Compare with `>=`, `>`, `<=`, `<` against a value |

### Actions

| Action | Description |
|---|---|
| Reset bet amount | Reset to base bet |
| Increase amount by % | Multiply current bet (e.g. 101% = ×2.01) |
| Decrease amount by % | Reduce current bet by percentage |
| Add to amount | Add a fixed value to bet |
| Deduct from amount | Subtract a fixed value from bet |
| Set amount | Set bet to an exact value |
| Switch over/under | Toggle bet direction (Dice only) |
| Stop betting | Stop the bot |
| Set win chance | Set exact win chance (recalculates multiplier) |
| Increase/Decrease win chance by % | Adjust win chance relatively |
| Reset game | Full reset (bet + streak counters) |

### Example

A Martingale variant:
- Every 1 win → Reset bet amount
- Every 1 loss → Increase amount by 101%
- Every 500 wins → Stop betting

---

## Named Presets

Save your strategy configuration and reuse it instantly:

```bash
# During wizard: last step asks to save as preset.

# List saved presets
python3 stake.py --list-presets

# Run with a saved preset (skips wizard)
python3 stake.py --preset my_strategy
```

Presets are stored in `~/.stake_presets.json`.

---

## Stop Conditions

Configure during setup wizard:

- **Max profit** — stop when session profit reaches X
- **Max loss** — stop when session loss exceeds X
- **Max bets** — stop after N bets
- **Max wins** — stop after N wins
- **Min balance** — stop if balance drops below X

---

## CLI Options

```bash
python3 stake.py                 # Interactive wizard + TUI dashboard
python3 stake.py --resume        # Skip wizard, reuse saved config
python3 stake.py --preset X      # Load preset X and start
python3 stake.py --daemon        # Run in background (no TUI, implies --resume)
python3 stake.py --monitor       # Attach live TUI to running daemon
python3 stake.py --status        # Check running session status (one-shot)
python3 stake.py --stop          # Stop a running daemon
python3 stake.py --setup-only    # Run wizard, save config, don't start
python3 stake.py --list-presets  # Show saved presets
python3 stake.py --stats         # Show all session history + totals
python3 stake.py --last-bets N   # Show last N bets across sessions
python3 stake.py --session-bets N  # Full stats + streak distribution for session N
```

---

## Keyboard Controls

| Key | Action |
|---|---|
| `P` | Pause betting |
| `R` | Resume betting |
| `H` | Show session history screen |
| `Q` | Stop bot and save session |
| `Ctrl+C` | Emergency stop (also saves) |
| `Ctrl+\` | Emergency stop (also saves) |

---

## Authentication

Stake.com uses Cloudflare protection. You need three values from your browser DevTools:

1. **x-access-token** — Found in request headers (Network tab → any stake.com API request)
2. **x-lockdown-token** — Found in request headers
3. **Cookie string** — Full cookie header (includes `cf_clearance`, `session`, `__cf_bm`, etc.)

To get these: Open DevTools (F12) → Network tab → place a manual bet → copy the values from the request headers.

---

## Files & Data

| Path | Purpose |
|---|---|
| `stake.py` | Main bot (single file) |
| `~/.stake_autobot.db` | SQLite database (sessions + bets) |
| `~/.stake_autobot.json` | Saved config for `--resume` |
| `~/.stake_presets.json` | Named strategy presets |
| `~/.stake_logs/stake.log` | Daily rotating log (30 days) |
| `~/.stake_autobot_live.json` | Live state for `--status` |
| `~/.stake_autobot.pid` | PID file for `--stop` |

```bash
# Query session history
sqlite3 ~/.stake_autobot.db "SELECT * FROM sessions ORDER BY id DESC LIMIT 5;"

# Bet stats
sqlite3 ~/.stake_autobot.db "SELECT state, COUNT(*), SUM(profit) FROM bets GROUP BY state;"
```

---

## API

Uses Stake.com's **GraphQL API** at `https://stake.com/_api/graphql`.

| Mutation | Purpose |
| --- | --- |
| `limboBet` | Place a Limbo bet |
| `diceRoll` | Place a Dice bet |

GraphQL avoids Cloudflare's REST endpoint protection — no `cf_clearance` cookie needed. Works from any VPS with just the access token.

---

## Disclaimer

This software is provided as-is for educational purposes. Gambling involves real financial risk. Never bet more than you can afford to lose. Use the stop conditions. Be responsible.
