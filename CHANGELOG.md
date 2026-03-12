# Changelog

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
