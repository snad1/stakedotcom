# Changelog

## v1.2.0 — Shared Library as pip Package (2026-03-29)

### Infrastructure

- **casino-shared pip package** — `shared/` library now installable via `pip install casino-shared` from GitHub, eliminating manual file copying on servers
- **requirements.txt** — Added `casino-shared @ git+ssh://git@github.com/snad1/casino-shared.git` to both bot and web requirements
- **install.sh** — Added `shared/` copy step as fallback for offline/non-pip installs
- **stakectl update** — Now syncs `shared/` library alongside core/tg during updates

---

## v1.1.9 — DRY Shared Library Extraction (2026-03-29)

### Infrastructure

- **Shared library extraction** — Extracted ~6,600 lines of duplicated code into `shared/` library used across all 6 casino bots
  - `web/websocket.py`, `web/database.py`, `web/auth.py`, `web/services.py`, `web/bot_db.py`, `web/routes/auth_routes.py` — thin shims delegating to shared modules
  - `tg/database.py` — thin shim binding `DATA_DIR` to shared persistence
  - `core/strategy.py` — keeps local STRATEGIES dict, delegates rule engine to shared
  - `core/database.py` — keeps local `init_db`, delegates utilities to shared
- Zero downstream import changes — all existing imports continue working

### Security

- **Shell injection fix** — Replaced `create_subprocess_shell` with `shutil.copy2` in update flow
- **Deprecated datetime fix** — Replaced `datetime.utcnow()` with `datetime.now(timezone.utc)`
- **Narrowed exception handling** — Replaced broad `except Exception` with specific types
- **Removed unused imports** — Cleaned up unused `import os` from shims

### Testing

- **114-test suite** added covering all shared modules

---

## v1.1.8 — Security Hardening (2026-03-29)

### CLI Bot v1.1.8

#### Security

- **`_save_state_file()` permissions** — State files now written with `0o600` permissions via `os.open()`, preventing other users from reading session state
- **ENV-controlled error messages** — TG bot error handlers now log full details server-side and show user-friendly messages in production. Controlled via `APP_ENV` environment variable
- **`stakectl` self-update safety** — Script wrapped in `{ }` block to prevent parse errors when `cmd_update` replaces the file mid-run

---

## v1.1.7 — Security, Rule Editor & Ops Improvements (2026-03-26)

### CLI Bot v1.1.7

#### Added

- **`_edit_one_rule()` interactive rule editor** — Edit existing rules in-place via the setup wizard; wired into the main configuration flow
- **`stakectl start-all / stop-all / status-all`** — Batch commands to start, stop, or check status of all managed services at once

#### Security

- **`_mask_key()` for token logging** — API tokens are masked before being written to logs in `save_config()`, preventing accidental credential exposure
- **`save_config()` file permissions** — Config files are now written with `0o600` permissions, restricting read access to the owning user only

### Infrastructure

- **`run-tg.sh` helper** — Added to `install.sh` for launching the Telegram bot process directly

---

## v1.1.6 — Data Retention & Cleanup (2026-03-24)

### TG Bot v1.1.6

#### Added

- **Automatic bet cleanup** — Old bet records (3+ days) from ended sessions are automatically purged when a new session starts. Session statistics (profit, bets, streaks, balance extremes, speed) are preserved — only raw bet rows are deleted. Disk space is reclaimed via VACUUM.
- **`/cleanup [days]`** — Manually purge bet records older than N days (default 3). Session stats remain intact. Usage: `/cleanup` or `/cleanup 1`.
- **`/delsession <id>`** — Delete a specific session and all its bets by session ID. Blocks deletion of currently running sessions. Shows bet count and profit before confirming.
- **`bets_purged` flag** — Sessions table gains a `bets_purged` column to track which sessions have already been cleaned, avoiding redundant queries on subsequent cleanups.

## v1.1.5 — Reliable Resume (2026-03-13)

### TG Bot v1.1.5

#### Fixed

- **Resume now tests API connection** — `start_resumed()` runs the full Cloudflare bypass chain before starting the betting loop, preventing silent 403 failures.
- **Resume retries** — Connection is retried up to 3 times (5s apart) on startup, handling cases where the API isn't ready immediately.
- **Resume failure notification** — If resume fails after retries, the user is notified via Telegram instead of silent failure.

## v1.1.4 — Resume Display Fixes (2026-03-13)

### TG Bot v1.1.4

#### Fixed

- **Worst Loss negative zero** — "Worst Loss: -0.00000000" no longer shows a spurious minus sign when loss is zero.
- **Speed 0 after resume** — BPS/BPM are now recalculated from session totals on resume instead of showing 0.

## v1.1.3 — Request Timeouts + Session Recovery (2026-03-13)

### TG Bot v1.1.3

#### Fixed

- **Request timeouts** — All API calls now use `(5s connect, 15s read)` timeout tuple to prevent indefinite hangs.
- **Automatic HTTP session recovery** — After 3 consecutive timeouts, the HTTP session is closed and recreated to recover from stale/dead connections.
- **Connection error handling** — `ConnectionError` exceptions are now caught alongside `Timeout`, preventing silent freezes from dropped TCP connections.

## v1.1.2 — Reliable Session Resume (2026-03-13)

### TG Bot v1.1.2

#### Fixed

- **Session resume on SIGTERM** — Added signal handler and atexit hook so sessions are reliably saved when the bot is stopped via `systemctl restart`. Previously `post_shutdown` wasn't always reached.

## v1.1.1 — Number Formatting + Input Parsing (2026-03-13)

### TG Bot v1.1.1

#### Improved

- **Comma-formatted number input** — `/set maxwins 36,000,000` now works. Commas are stripped from all numeric `/set` values.
- **Readable number output** — All integer stats (bets, wins, losses, streaks, session counts) now display with comma separators (e.g. `276,000` instead of `276000`).

## v1.1.0 — TG Bot: Multi-Session + Fixes (2026-03-13)

### TG Bot v1.1.0

#### Added

- **Multi-session support** — Run up to 5 concurrent betting sessions per user. Each session gets its own slot number. Use `/bet` multiple times to start additional sessions.
- **Slot-based commands** — `/stop 2`, `/status 2`, `/pause 2`, `/resume 2` to target specific sessions. Single session auto-resolves (no slot needed).
- **Bulk control** — `/stop all`, `/pause all`, `/resume all` to control all sessions at once.
- **Multi-session status summary** — `/status` shows compact overview when multiple sessions are running.
- **`/editrule <N> <json>`** — Edit an existing rule by number with a JSON patch (merge into current fields).
- **Config snapshot in session history** — `/session <id>` shows the exact strategy, rules, stops, and config used at session start.
- **Strategy-aware `/config`** — Only shows relevant fields per strategy (e.g. loss_mult for Martingale, delay_threshold for Delay Martingale, rules for Rule-Based).
- **21 rule actions** — Added all missing actions from Stake's autobet UI: reset/set/increase/decrease/add/deduct for win chance and payout, reset_game.
- **Zero-downtime updates** — `stakectl update` now auto-restarts TG bot with session auto-resume. Active sessions pause, save state, and resume after restart.

#### Fixed

- **`/delrule`** — Now correctly deletes rules by number.
- **`migrate_db` import error** — Fixed `ImportError: cannot import name 'migrate_db'` that crashed the TG bot on startup.
- **Milestone notifications** — Fixed silent error swallowing that prevented milestone callbacks from firing.
- **Smart quotes in `/addrule`** — Telegram auto-replaces `"` with curly quotes, breaking JSON. Now sanitized automatically.
- **Rule trigger normalization** — "lose" → "loss", "wins" → "win", "losses" → "loss", "bets" → "bet" — rules now match regardless of how the trigger is typed.
- **`/set basebet 0`** — Fixed falsy value override (`0` was treated as missing and defaulted to `0.0001`).
- **Insufficient balance** — Bot now stops session cleanly when API returns insufficient balance error instead of looping.

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
