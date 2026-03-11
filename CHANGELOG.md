# Changelog

## v1.1.0 (2026-03-11)

### New Features

- **Dice game support** — Full Dice game implementation with target number + above/below condition. Automatic target/multiplier calculation.
- **Multi-game architecture** — Pluggable game registry via `_register_game()`. Each game defines its own endpoint, payload builder, and result parser. Adding a new game takes ~20 lines of code.
- **Game selection in wizard** — Step 1 now asks which game to play (Limbo or Dice). Dice prompts for target and condition (above/below).
- **Cloudflare cookie bypass** — Added full Cookie header passthrough to handle Stake.com's Cloudflare protection. Wizard prompts for browser cookie string.

### Enhancements

- **Bets table extended** — Added `game TEXT` and `result_display TEXT` columns with automatic migration for existing databases.
- **Per-game dashboard** — Result column shows game-specific data: `2.31x` for Limbo, `61.20` for Dice.

## v1.0.0 (2026-03-11)

### New Features

- **Limbo game** — Auto-betting on Stake.com Limbo with configurable multiplier target.
- **Pure ANSI TUI** — Direct ANSI rendering dashboard (no Rich flicker), alternate screen buffer with proper terminal state save/restore.
- **7 betting strategies** — Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, Delay Martingale, Rule-Based.
- **Rule-based engine** — Build custom IF/THEN rules with interactive wizard. 3 condition types (Sequence, Profit, Bet) and 12 actions.
- **Customizable multipliers** — Strategies 2, 3, 5, 6 prompt for loss/win multiplier in the wizard.
- **Named presets** — Save & load strategy configs by name. `--preset NAME`, `--list-presets`.
- **Smart rate recovery** — Exponential probe system instead of hard cooldown.
- **Monitor mode** (`--monitor`) — Attach a live TUI to a running daemon. Pause/resume/stop remotely.
- **Daemon mode** (`--daemon`) — Background running with `--status` to check and `--stop` to halt.
- **Session database** — SQLite log of every bet and session, survives restarts.
- **Config persistence** — Save/load with `--resume`, smart wizard with defaults.
- **Performance tracking** — BPS, BPM, peak/low speed ranges, peak/low balance, best win, worst loss, avg P/L.
- **Streak distribution** — `--session-bets N` shows win/loss streak frequency bar charts.
- **Test mode** — Set bet amount to `0` for free practice bets.
- **Stop conditions** — Max profit, max loss, max bets, max wins, min balance floor.
- **Safety cap** — 20% balance cap prevents runaway bets.
- **Daily log rotation** — `~/.stake_logs/stake.log` with midnight rotation, 30-day retention.
- **Interactive keyboard** — `P` pause, `R` resume, `H` history, `Q` quit from dashboard.
