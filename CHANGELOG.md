# Changelog

## v1.7.1 — Fix _safe_send infinite recursion (2026-05-02)

### Fixed

- **`_safe_send` infinite recursion** — The function was calling itself instead of `app.bot.send_message`, causing `RecursionError: maximum recursion depth exceeded` on every Telegram notification (stops, milestones, errors). Fixed to call `app.bot.send_message` directly.
- **curl_cffi `AsyncSession.close()` unawaited** — `self._http.close()` is a coroutine when using curl_cffi but was called without `await`, generating `RuntimeWarning`. Now detected and awaited properly.

## v1.7.0 — Recurring honors live config across all running sessions (2026-04-28)

### Fixed

- **Recurring only applied to sessions started AFTER `/set recurring on`** — Previously, `recurring_state` was registered at `/bet` time only if `config.get("recurring")` was true at that moment. If a user started a session, then later toggled recurring on and started another session, only the second one would auto-restart on stop — the first had no recurring entry. Now `recurring_state` is registered for every session unconditionally (per-slot snapshot of game/strategy preserved). At stop time the engine re-reads the user's saved config to decide whether to actually fire the restart, so toggling `/set recurring on/off` applies instantly to **every** running session regardless of game type.
- **Recurring lost across bot restart** — `load_resume_state` now also registers `recurring_state` for each resumed session so a stop after a bot update still triggers the configured restart.
- **Notification throttle now driven by live config** — The "is this a recurring session" check used for stop-message rate-limiting now reads the live `recurring` flag instead of the snapshot, matching the new behavior.

Per-session game/strategy isolation is preserved: each session's snapshot keeps its own game/strategy/rule etc., so a session restart stays the same game even with multiple games running concurrently.

## v1.6.0 — Streak Delay Bets (2026-04-21)

### Added

- **`streakdelay_bets`** — Delay every N total bets by X seconds, regardless of win/loss outcome. Format `N:seconds` (e.g. `/set streakdelay_bets 100:0.5` pauses 0.5s every 100 bets). Useful for throttling overall bet rate independent of streaks. Live-tweakable via `/tweak sdbets 100:0.5`. When multiple streak delays apply on the same bet (e.g. loss streak + bet count), the longest delay wins

## v1.5.2 — Recurring Notification Throttle (2026-04-21)

- **Telegram flood ban on fast recurring sessions** — Stop and start notifications are now rate-limited to at most one of each per 30 seconds per user during recurring cycles. Losses, errors, insufficient balance, and manual stops are always sent immediately regardless of throttle

## v1.5.1 — Recurring Restart Reliability & Task Safety (2026-04-21)

### Fixed

- **Fire-and-forget task exceptions now logged** — Added `_log_task_exception` done-callback to all `create_task` calls so unhandled exceptions are surfaced in logs instead of being silently swallowed
- **`_notify_stop` formatting errors caught** — Wrapped `format_stop` call in try/except; falls back to plain stop message so the bot never crashes on a bad status snapshot
- **`ensure_future` replaced with `create_task`** — All callback factories (`_make_on_stop`, `_make_on_milestone`, `_make_on_error`) now use `asyncio.create_task` with done-callbacks for proper error visibility
- **Recurring restart retries on engine-start failure** — When `engine.start()` returns false, the session is re-queued after the configured delay instead of being silently dropped
- **Monitor loop stays alive during restart gap** — `_monitor_loop` now continues iterating while a recurring restart timer is pending, showing "Recurring restart pending…" instead of prematurely exiting
- **Inline keyboard preserved during restart gap** — `refresh_status` callback keeps Refresh/Stop buttons visible with a pending message while waiting for the next recurring restart

## v1.5.0 — Adaptive Base Bet & Full Config Visibility (2026-04-14)

### Added

- **`basebet_pct`** — Set base bet as a fraction of current balance (e.g. `/set basebet_pct 0.001` = 0.1%). Recomputes on session start and at every profit-increment milestone, so base bet auto-shrinks during drawdowns and grows during profit. Live-tweakable via `/tweak bbpct 0.001`
- **`streakbet_loss`** — Reduce (or scale) bet after N consecutive losses. Format `N:multiplier` (e.g. `/set streakbet_loss 10:0.5` halves bet every 10 losses). Damping floor for Martingale-style strategies. Live-tweakable via `/tweak sbloss 10:0.5`

### Changed

- **Full config in every surface** — Session-start message, `/status`, and stop summary now display every active setting: streak delays, streak bet, basebet pct, profit bump + next milestone, milestone cadence, stops, win/loss multipliers. Single `format_full_config` helper in formatter; no more drift between surfaces

## v1.4.4 — Insufficient Balance Stop (2026-04-13)

### Fixed

- **Insufficient balance now always stops session** — Added pre-bet balance guard (`current_bet > current_balance`). Previously, deep Martingale streaks could drive `current_bet` above balance without the session stopping

## v1.4.3 — Streak Delay & Recurring Bet Fixes (2026-04-09)

### Added

- **Streak delay** — Delay next bet after N consecutive wins/losses. Configure with `/set streakdelay_loss 5:1.0` (every 5 losses → 1s delay) and `/set streakdelay_win 10:0.5`. Works with all strategies including auto-bet. Live tweak with `/tweak sdloss 5:1.0` / `/tweak sdwin off`
- **Streak delay in /config** — Displays streak delay settings in configuration output

### Fixed

- **Recurring bet preserves profit increment** — When a session stops and recurring restarts, the current (profit-incremented) base_bet is now carried forward instead of resetting to the original config value. Useful for short recurring sessions to bypass server-side throttling while maintaining bet progression

## v1.4.2 — Bug Fixes (2026-04-07)

### Fixed

- **Live bet cleanup** — ISO timestamp `T` separator caused comparison failure with SQLite's `datetime()`. Bets older than purge_days now correctly deleted during running sessions
- **Telegram timeout on session start** — Session start confirmation wrapped in try/except. Session continues even if Telegram times out
- **Help text** — Added missing cleanup, delsession, purgedays commands

---

## v1.4.1 — Safety, Cleanup, Charts (2026-04-03)

### Added

- **Live bet cleanup** — Bets older than N days auto-deleted every hour during running sessions. `/set purgedays 1` (default 1, range 1-30)
- **Chart snapshots** — Profit/balance saved every 100 bets. Web charts survive bet cleanup
- **Profit increment = 0** — Creates new sessions at profit threshold without increasing base bet

### Fixed

- **CK audit** — 65 scientific notation violations fixed across all services
- **`/tweak basebet` safety** — No longer kills strategy recovery during loss streak (applies on next win)

---

## v1.4.0 — Async Engine, /tweak, Proxy (2026-04-02)

### Added

- **Async engine** — TG betting engine converted from blocking threads to async I/O. Multiple sessions run truly concurrent instead of competing for Python's GIL
- **`/tweak` command** — Live-edit running sessions without stopping: delay, stop conditions (maxwins, maxbets, maxprofit, maxloss, minbalance), milestones, basebet, multiplier, loss/win mult
- **Per-session proxy** — Route each session through a different IP: `/set proxy http://user:pass@ip:port` or `socks5://...`
- **Production-safe errors** — `_set_error()` method ensures raw API errors never leak to users in production

### Changed

- HTTP client: `requests.Session` → `curl_cffi.AsyncSession`
- Threading: `threading.Thread` → `asyncio.create_task`
- Sleep: `time.sleep` → `asyncio.sleep`
- Callbacks: `run_coroutine_threadsafe` → `asyncio.ensure_future`
- Suppressed httpx/httpcore polling log noise

---

## v1.3.2 — Recurring Bets & Zero-Balance Fix (2026-03-29)

### Added

- **Recurring bet feature** — Sessions auto-restart after configurable delay when stopped by conditions (profit target, max bets, etc). Configure with `/set recurring on|off` and `/set recurringdelay <seconds>`. Cancel with `/stop recurring`

### Fixed

- **TG zero-balance start** — TG engine now allows zero-balance sessions when base bet is 0 (previously blocked with "No balance found")

---

## v1.3.1 — Fix BPM Calculation (2026-03-29)

### Fixed

- **BPM calculation** — `bets_per_minute` was set to raw count of bets in the current calendar minute instead of the actual rate; now correctly derived as `bets_per_second * 60`

---

## v1.2.1 — Zero Bets & Insufficient Balance Fix (2026-03-29)

### Fixed

- **Zero-amount bets** — Allow base bet of 0 for free/test betting; MIN_BET guard changed from `amount < MIN_BET` to `0 < amount < MIN_BET` so zero passes through to the API
- **Default base bet** — Changed default `base_bet`/`current_bet` from `MIN_BET` to `0.0`
- **Insufficient balance detection** — CLI and TG now properly detect "insufficient balance" in HTTP error responses and stop the session immediately
- **TG error notification** — Added `on_error` callback; sends `⚠️ Insufficient balance` message to user via Telegram before stopping

---

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
