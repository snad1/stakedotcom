# Stake AutoBot v1.2

High-speed multi-game auto-betting engine for Stake.com (Limbo + Dice) with a live terminal dashboard. Includes Cloudflare bypass for server via FlareSolverr + curl_cffi Chrome TLS fingerprinting.

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
| **Cloudflare bypass** | 3-pass chain: Direct → Cached CF cookies → FlareSolverr headless Chrome solve |
| **CF cookie caching** | Persists solved cookies to `~/.stake_cf_cookies.json` with 30-min TTL |
| **Multi-domain fallback** | Tries stake.bet first (lighter CF), falls back to stake.com |
| **Balance API** | Fetches real balances via GraphQL `UserBalances` query |
| **Smart stop conditions** | Max profit, max loss, max bets, max wins, min balance floor |
| **Safety cap** | Never bets more than 20% of current balance in one go |
| **Session database** | SQLite log of every bet and session — survives restarts |
| **Config persistence** | Save/load config with `--resume`, smart wizard with defaults |
| **Monitor mode** | `--monitor` to attach a live TUI to a running daemon — pause/resume/stop remotely |
| **Daemon mode** | `--daemon` for background running, `--status` to check, `--stop` to halt |
| **Performance tracking** | BPS, BPM, peak/low speed ranges, peak/low balance, best win, worst loss, avg P/L |
| **Streak distribution** | Per-session bar charts showing win/loss streak frequency |
| **Uptime tracking** | Session history shows computed uptime for each session |
| **Test mode** | Set bet amount to `0` for free bets (no real money risked) |
| **Daily log rotation** | Logs at `~/.stake_logs/stake.log`, rotates at midnight, 30-day retention |
| **Interactive keyboard** | `P` pause, `R` resume, `H` history, `Q` quit — all from dashboard |

---

## Quick Start

### Mac / Local

```bash
# 1. Setup
cd stake
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Run (wizard guides you through config)
python3 stake.py

# 3. You'll need from your browser DevTools (F12 → Network tab → any stake.com request):
#    - x-access-token (from request headers)
#    - x-lockdown-token (from request headers)

# 4. Resume with saved config
python3 stake.py --resume
```

### Remote Server

```bash
# 1. Upload files
scp stake.py requirements.txt install.sh user@your-server:~/

# 2. Install (sets up venv, systemd, Docker, FlareSolverr)
ssh user@your-server
chmod +x install.sh && ./install.sh

# 3. Configure
stakectl setup

# 4. Start & monitor
stakectl start
stakectl monitor
```

---

## Cloudflare Bypass (server)

Stake.com uses Cloudflare which blocks datacenter IPs. The bot uses a 3-pass connection strategy:

1. **Direct** — Try connecting without any CF cookies (works on residential IPs)
2. **Cached cookies** — Load previously solved CF cookies from `~/.stake_cf_cookies.json` (30-min TTL)
3. **FlareSolverr** — Solve fresh Cloudflare challenge via headless Chrome, extract cookies + user-agent, cache to disk

The solved cookies are paired with `curl_cffi` (Chrome TLS fingerprint) and the matching user-agent from FlareSolverr to avoid TLS fingerprint mismatches.

### FlareSolverr Setup

```bash
# Install Docker (if not already installed)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for group change

# Start FlareSolverr
docker run -d --name flaresolverr \
  -p 8191:8191 \
  --restart unless-stopped \
  ghcr.io/flaresolverr/flaresolverr:latest

# Verify it's running
curl -s http://localhost:8191/v1 | head -c 100
```

The bot auto-detects FlareSolverr at `http://localhost:8191/v1` and uses it when direct connections fail. The `install.sh` script handles Docker + FlareSolverr setup automatically.

---

## Games

| Game | Endpoint | Parameters |
|---|---|---|
| **Limbo** | `POST /_api/casino/limbo/bet` | `multiplierTarget`, `amount`, `currency`, `identifier` |
| **Dice** | `POST /_api/casino/dice/roll` | `target`, `condition` (above/below), `amount`, `currency`, `identifier` |

### Dice target math

- **Above**: `target = 100 - 99/multiplier` (e.g. 2x → 50.5)
- **Below**: `target = 99/multiplier` (e.g. 2x → 49.5)

### Adding new games

Games are registered via `_register_game(name, label, endpoint, response_key, build_payload, parse_result)`. Define a payload builder and result parser, then register — ~20 lines of code.

---

## Dashboard

Pure ANSI rendering — pinned rows + adaptive recent bets. Fits any terminal.

```text
─── Stake v1.2  Limbo  #3  00:12:34  ● LIVE ──────────────────────
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
python3 stake.py                   # Interactive wizard + TUI dashboard
python3 stake.py --resume          # Skip wizard, reuse saved config
python3 stake.py --preset X        # Load preset X and start
python3 stake.py --daemon          # Run in background (no TUI, implies --resume)
python3 stake.py --monitor         # Attach live TUI to running daemon
python3 stake.py --status          # Check running session status (one-shot)
python3 stake.py --stop            # Stop a running daemon
python3 stake.py --setup-only      # Run wizard, save config, don't start
python3 stake.py --list-presets    # Show saved presets
python3 stake.py --stats           # Show all session history + totals + uptime
python3 stake.py --last-bets N     # Show last N bets across sessions
python3 stake.py --session-bets N  # Full stats + streak distribution for session N
```

---

## stakectl (server Management)

After running `install.sh`, the `stakectl` command is available:

```bash
stakectl setup          # Run wizard to configure
stakectl start          # Start bot as background daemon
stakectl stop           # Stop the bot
stakectl restart        # Restart the bot
stakectl monitor        # Attach live TUI to running daemon
stakectl status         # Quick status snapshot
stakectl logs           # Stream live logs
stakectl logs-full      # Show last 200 log lines
stakectl interactive    # Start daemon + attach monitor
stakectl tmux           # Monitor in detachable tmux session
stakectl stats          # View all-time statistics
stakectl session ID     # Full stats + streak distribution
stakectl presets        # List saved presets
stakectl update         # Update bot from current directory
```

---

## Keyboard Controls

| Key | Action |
|---|---|
| `P` | Pause betting |
| `R` | Resume betting |
| `H` | Show session history screen |
| `Q` | Stop bot and save session |
| `S` | Stop (in monitor mode) |
| `Ctrl+C` | Emergency stop (also saves) |
| `Ctrl+\` | Emergency stop (also saves) |

---

## Files & Data

| Path | Purpose |
|---|---|
| `stake.py` | Main bot (single file) |
| `~/.stake_autobot.db` | SQLite database (sessions + bets) |
| `~/.stake_autobot.json` | Saved config for `--resume` |
| `~/.stake_presets.json` | Named strategy presets |
| `~/.stake_logs/stake.log` | Daily rotating log (30 days) |
| `~/.stake_autobot_live.json` | Live state for `--status` / `--monitor` |
| `~/.stake_autobot.pid` | PID file for `--stop` |
| `~/.stake_cf_cookies.json` | Cached Cloudflare cookies (30-min TTL) |

```bash
# Query session history
sqlite3 ~/.stake_autobot.db "SELECT * FROM sessions ORDER BY id DESC LIMIT 5;"

# Bet stats
sqlite3 ~/.stake_autobot.db "SELECT state, COUNT(*), SUM(profit) FROM bets GROUP BY state;"
```

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /_api/casino/limbo/bet` | Place Limbo bet |
| `POST /_api/casino/dice/roll` | Place Dice bet |
| `POST /_api/graphql` | Fetch user balances (GraphQL `UserBalances` query) |

Base URLs: `https://stake.bet/_api/casino` (primary), `https://stake.com/_api/casino` (fallback)

Auth headers: `x-access-token`, `x-lockdown-token` (from browser DevTools)

---

## Disclaimer

This software is provided as-is for educational purposes. Gambling involves real financial risk. Never bet more than you can afford to lose. Use the stop conditions. Be responsible.
