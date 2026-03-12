# Changelog

## v1.2.2 — TG Bot Fixes (2026-03-12)

### Fixed

- **`/balance` Cloudflare bypass** — Now runs full 3-pass CF chain (direct → cached cookies → FlareSolverr solve) instead of raw requests that got blocked
- **Missing `API_BASES` import** — `/balance` crashed with `name 'API_BASES' is not defined`
- **`/help` profit increment** — Added `(off to disable)` hint for `profitthreshold` and `profitincrement`

### Added

- **`.env.example`** — Environment template with auth tokens, TG token, file paths, and BotFather setup guide
- **`stakectl tg env`** — Edit `.env` file and sync TG token to systemd
- **Better error messages** — `/balance` shows which domains were tried on failure
- **Balance response logging** — Debug logging for GraphQL responses

## v1.2.1 — Telegram Bot (2026-03-12)

### Added

- **Telegram Bot v1.0** (`stake/tg/`) — Full multi-tenant Telegram bot for Stake auto-betting
- **Multi-game support** — Limbo + Dice via game registry, switchable with `/set game`
- **22 commands** — `/settoken`, `/balance`, `/config`, `/set`, `/strategies`, `/bet`, `/stop`, `/pause`, `/resume`, `/status`, `/monitor`, `/stats`, `/session`, `/lastbets`, `/rules`, `/addrule`, `/clearrules`, `/presets`, `/savepreset`, `/loadpreset`, `/help`, `/start`
- **Live monitor** — Auto-updating status messages with inline buttons (3-60s intervals)
- **Shared core module** (`stake/core/`) — Strategy, database, and engine logic shared between CLI and TG bot
- **Cloudflare bypass chain** — 3-pass: direct → cached CF cookies → FlareSolverr headless solve (per-user)
- **Batched DB writes** — Flush every 50 bets via `executemany()`, session stats saved on each flush
- **Cross-thread SQLite safety** — Temp connection for session creation (main thread), lazy persistent connection in betting thread
- **Periodic session save** — Stats saved every 30s without setting `ended_at` (running sessions appear as running)
- **Zombie session cleanup** — `/stop` cleans up sessions with NULL `ended_at` from prior crashes
- **Callback-safe replies** — `_reply()` helper handles both `/command` and inline button contexts
- **None-safety** — All config reads use `or` pattern to handle `None` values from presets
- **Full ISO timestamps** — Microsecond precision in all DB writes, displays, and calculations
- **Profit-based base bet increment** — Auto-raise base bet every X profit
- **Milestone notifications** — Configurable alerts at N bets/wins/losses/profit intervals
- **BPS/BPM tracking** — Peak and low speed ranges tracked per session
- **Preset security** — Presets exclude sensitive tokens (access_token, lockdown_token, cookie)
- **Per-user isolation** — Separate DB, config, presets, and CF cookies per Telegram user

## v1.2.0 (2026-03-11)

### Added
- **Cloudflare bypass chain**: 3-pass connection — Direct → Cached CF cookies → FlareSolverr (headless Chrome) → curl_cffi with Chrome TLS fingerprint + matching user-agent
- **CF cookie caching**: Persist solved cookies to `~/.stake_cf_cookies.json` with 30-minute TTL — avoids re-solving on every restart
- **Multi-domain fallback**: Auto-detects working domain (tries stake.bet first, falls back to stake.com)
- **Balance API**: Fetch real balances via GraphQL `UserBalances` query
- **Monitor mode** (`--monitor`): Attach live TUI to a running daemon — pause/resume/stop remotely
- **Session bets** (`--session-bets ID`): Full stats + streak distribution for a specific session
- **Last bets** (`--last-bets N`): Show last N bets across all sessions
- **Uptime tracking**: Session history and session detail views show computed uptime (hours/minutes/seconds)
- **Enhanced `--stats`**: Detailed session history with speed metrics, balance peaks, streaks, uptime
- **Enhanced `--status`**: Rich one-shot status display with all session metrics
- **server installer**: `install.sh` with systemd service, `stakectl` management CLI, Docker/FlareSolverr setup

### Changed
- HTTP client chain: curl_cffi (Chrome TLS) → cloudscraper → plain requests
- Connection test uses 3-pass fallback with automatic domain switching
- Headers include CF cookies and matching user-agent from FlareSolverr
- Switched from GraphQL mutations to REST endpoints for bets (REST works reliably with CF bypass)
- GraphQL used only for balance query

### Fixed
- **403 Forbidden on server**: Solved via FlareSolverr cookie extraction + curl_cffi Chrome TLS fingerprinting on same IP

## v1.1.0 (2026-03-10)

### Added
- **Dice game support** — Full Dice game with target number + above/below condition
- **Game registry pattern** — `_register_game()` with per-game endpoint, payload builder, response parser
- **Game selection in wizard** — Choose Limbo or Dice at setup
- **Cloudflare cookie passthrough** — Cookie header support for CF-protected endpoints

### Changed
- Renamed from "Limbo AutoBot" to "Multi-Game Auto-Betting Engine"
- Bets table extended with `game` and `result_display` columns (auto-migrated)

## v1.0.0 (2026-03-09)

### Initial Release
- Limbo game support on Stake.com
- 7 betting strategies: Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, Delay Martingale, Rule-Based
- Rule-based strategy with interactive wizard
- Named presets system
- Pure ANSI TUI dashboard with 2fps refresh
- SQLite session + bet logging
- Daemon mode with `--resume`, `--status`, `--stop`
- Smart stop conditions (profit, loss, bets, wins, min balance)
- 20% balance safety cap
- Daily log rotation (30 days)
- Config persistence
