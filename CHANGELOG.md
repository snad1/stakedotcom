# Changelog

## v1.9.10 — `/status` speed block trimmed (2026-06-17)

### Changed

- **Removed `Overhead` line** from the `/status` speed block — rarely actionable at a glance, adds noise.
- **`API stable` and `Efficiency` now render only when `bet_delay > 0`.** With no delay set, the theoretical-max reference is meaningless and median-vs-target has no target — both lines were pure clutter. When the user does set a delay, both lines render as before.

## v1.9.9 — Session-ended notification: green ✅ on profit, red 🛑 on loss (2026-06-17)

### Changed

- **`Session #X ended` notification now leads with `✅`** when the session ended with `profit >= 0`, keeping `🛑` for `profit < 0`. Previously every stop-notification opened with `🛑` regardless of outcome — a `max_profit` hit still triggered a red-icon push. Leading emoji is now driven by the sign of `s["profit"]`; every other line is unchanged.

## v1.9.8 — Fix: `/status` rendering as plain text — `bet_delay` opened an unclosed italic entity (2026-06-17)

### Fixed

- **`/status` rendered as PLAIN TEXT** (literal `*Session #X*`, `*Balance*`, `` `…` `` visible) because the v1.9.7 fallback was firing every invocation. Root cause: the literal token `bet_delay` on the "API stable" line sat OUTSIDE any code span — Markdown V1 read the `_` as the start of an italic entity, never found the closing `_`, and rejected the message. Renamed to `bet delay` (space, not underscore) — same meaning, no Markdown bomb. The v1.9.7 fallback remains in place as the safety net.

## v1.9.7 — Fix: `/status` silently failing on Markdown parse error (2026-06-17)

### Fixed

- **`/status` returns nothing in Telegram** when a rule description or engine status string contains an unbalanced Markdown character. `format_status` wraps user/external strings inside Markdown V1 code spans (`` `…` ``); a stray backtick splits the entity and Telegram silently rejects the message. Fixed at the render boundary — `_safe_code(s)` escapes backticks in the two leaky fields (rule descriptions, trailing engine status). Added `safe_reply_markdown(...)` wrapper for `/status` and `/monitor`; enhanced `_safe_send` and the monitor-refresh `edit_text` paths to retry as plain text on parse-error `BadRequest`. The user always sees the message; technical detail is logged server-side only.

## v1.9.6 — Fix: SQLite cross-thread error in _get_conn (defensive) (2026-06-17)

### Fixed

- **`DB flush_bets failed: SQLite objects created in a thread can only be used in that same thread.`** — `_get_conn` now opens directly with `sqlite3.connect(..., check_same_thread=False)` instead of via shared `db_connect`, so a stale pip-installed casino-shared can't reintroduce the bug.

## v1.9.5 — API stable: median + on-target % (2026-06-17)

### Added

- **`API stable: Xms median / Y% on-target`** — median = typical API time (unbiased by spikes); on-target % = bets where API ≤ bet_delay. Explains the Efficiency ceiling: bps deficit ≈ (100% − on_target%).

## v1.9.4 — Efficiency % indicator (2026-06-17)

### Added

- **`Efficiency: X%`** on the dashboard — bot's actual recent bps as a % of the theoretical max (= 1 / max(bet_delay, api_avg)). Single number that answers "is the bot at peak?". The line includes the theoretical max bps so the ceiling is visible.

## v1.9.3 — API peak time on the dashboard (2026-06-17)

### Added

- **`API: X last / Y avg / Z peak`** — third field tracks the slowest single API response observed this session. Permanently records spikes so users can see WHY `Cycle` is elevated above `bet_delay`.

## v1.9.2 — Cycle metric: full per-bet time (bps reciprocal) (2026-06-17)

### Added

- **`Cycle: Xms/bet`** on the dashboard — exp-smoothed time between consecutive bet starts. This is the bps reciprocal (`Cycle × bps ≈ 1000`), shown inline as `(1000/385 = 2.60 bps)`. Honest measurement of what's actually happening per bet; complements `Overhead` (which only measures post-API processing).

## v1.9.1 — Per-cycle overhead diagnostic + get_status() cache (2026-06-17)

### Added

- **`Overhead: Xms/bet`** on the dashboard — exp-smoothed time per bet OUTSIDE of `bet_delay` and the API call. Healthy = ~5–15ms; 50ms+ = investigate.
- **`get_status()` cache (250ms TTL)** — eliminates the brief per-second bucket dip at milestones.

## v1.9.0 — Sliding-window BPS + non-blocking SQLite writes (2026-06-17)

### Added

- **`Now` bps (sliding 60s window)** alongside the existing cumulative `Avg`. Dashboard now shows BOTH so you can tell what the bot is doing RIGHT NOW vs. the lifetime average.

### Changed

- **DB writes no longer block the event loop.** `_flush_bets`, `_db_save_session`, and `cleanup_live_bets` (if present) now run via `asyncio.to_thread`. Inline batch-size flush trigger removed — flushes are now driven exclusively by `_periodic_save` every 30s. Shared `db_connect` opens with `check_same_thread=False` so the connection is safe to use from worker threads.

Result: multiple concurrent sessions now scale closer to single-session throughput, and the dashboard's `Now` number reflects current speed rather than session average.

## v1.8.0 — Auto-revive: systemd retry-forever + external stall watchdog (2026-06-17)

### Added

- **`Restart=always` + `StartLimitIntervalSec=0`** on both CLI and TG bot services. Bot now restarts after every exit (not just non-zero), and systemd never stops retrying after N quick failures.
- **External stall watchdog timer** — A new `stake-tg-watchdog.timer` fires every 2 minutes and runs a bash check: if `/tmp/stake-tg.heartbeat` is missing or older than 180s, it forces `systemctl restart stake-tg`. Catches the stuck-but-alive case (process running, event loop frozen) that `Restart=` can't catch.
- **Heartbeat task in the bot** — `_heartbeat_loop()` runs as an asyncio background task started from `post_init`, writing a fresh mtime to the heartbeat file every 30 seconds.

Result: if the bot crashes, systemd brings it back in 10s. If the bot stalls, the watchdog brings it back within ~3 minutes. No more SSH-and-restart.

## v1.7.20 — /update cd's into repo dir before running ctl (2026-06-11)

### Fixed

- **/update failed with "No stake.py in current directory"** — `systemd-run` spawns the bash command with cwd=/ (not the bot's repo dir). The `stakectl update` script looks for the bot py file in cwd to copy. Now `cd /root/stake && stakectl update` is the bash command. Override via `BOT_REPO_DIR` env var if your layout is different.

## v1.7.19 — /update reports real errors + writes update log (2026-06-10)

### Fixed

- **/update silently failed when systemd-run errored** — Previous `subprocess.Popen` with `stderr=DEVNULL` swallowed failures. Now uses `subprocess.run --no-block` synchronously, captures stdout/stderr, surfaces real errors to Telegram. Update output goes to `/tmp/stakectl-update.log` so you can review what happened: `cat /tmp/stakectl-update.log`.

## v1.7.18 — /update Telegram command for self-update (2026-06-10)

### Added

- **`/update` Telegram command (owner-only)** — Pull latest code from git, copy files, reinstall `casino-shared`, restart the bot. Equivalent to running `stakectl update` over SSH, but triggered from Telegram. Owner gate via `TG_OWNER_ID` env var (defaults to your user ID if unset). Implementation spawns `stakectl update` via `systemd-run --user --collect` so the update survives the bot getting killed by the restart.

## v1.7.17 — Remove 5-session concurrency limit (2026-05-12)

### Changed

- **Unlimited concurrent sessions per user** — `MAX_SESSIONS = 5` cap removed from `/bet` and from the recurring-restart path. Users can now run as many concurrent sessions as they want (limited only by the casino's own API rate limits, which are shared across sessions).

## v1.7.16 — bet_delay is a minimum-interval, not a sleep-before (2026-05-11)

### Changed

- **`bet_delay` semantics** — Previously: `sleep(bet_delay)` before each bet, so cycle = `bet_delay + api_time`. Now: minimum interval since the previous bet *started*, so cycle = `max(bet_delay, api_time)`. With delay=1s and api=50ms, you get a clean 1 bet/sec; with api=1500ms there's no extra sleep so the bot keeps up. More predictable pace.

## v1.7.15 — `warp-setup.sh` handles stale registration (2026-05-04)

### Fixed

- **`Old registration is still around` blocked re-runs** — Detects the existing-registration error, runs `warp-cli registration delete`, retries. Also uses `warp-cli registration show` (not `account`) to check for an existing valid registration.

## v1.7.14 — `warp-setup.sh` ensures warp-svc daemon is running (2026-05-04)

### Fixed

- **`warp-cli` fails with "No such file or directory" when daemon stopped** — If the WARP daemon was previously disabled (e.g. during SSH lockout recovery), `warp-cli registration new` couldn't reach it. Script now `systemctl enable warp-svc && systemctl start warp-svc` and waits up to 15s for the daemon socket before continuing.

## v1.7.13 — `warp-setup.sh` now uses proxy mode (SSH-safe) (2026-05-02)

### Fixed

- **`warp-setup.sh` locked users out of SSH** — Default WARP mode is full-tunnel: it captures ALL outbound traffic, including SSH return packets, which broke the management connection. Script now sets `warp-cli mode proxy` BEFORE connecting, so WARP runs as a local SOCKS5 server on `127.0.0.1:40000` and only the bot opts in. SSH and everything else stay untouched.
- **Run bot via WARP proxy** — `python3 stake.py --proxy socks5://127.0.0.1:40000` routes only the bot's traffic through WARP's Cloudflare-owned exit IP.

## v1.7.12 — Free CF-block fixes: WARP setup + CF Worker proxy (2026-05-02)

### Added

- **`warp-setup.sh`** — Installs Cloudflare WARP (free, no signup, no email). Routes all server traffic through Cloudflare's network which CF often trusts more than datacenter IPs. 30-second setup; usually fixes IP-banned servers without paying.
- **`cloudflare-worker/proxy.js`** — Drop-in JS for a Cloudflare Worker that transparently proxies the bot's requests to stake.com. Free tier supports 100K requests/day. Workers run on CF's network so requests bypass IP-based bot scoring entirely. Includes 5-minute deployment guide.
- **`--api-base` flag + `STAKE_API_BASE` env var** — Override the upstream stake.com URL with your Worker URL: `python3 stake.py --api-base https://my-bot.example.workers.dev`. The bot's connection test, betting, and balance queries all route through the Worker.

### Changed

- **Final CF-blocked error now leads with free options** — WARP, then Worker, then run-locally, then ProtonVPN free tier. Paid options moved out.

## v1.7.11 — patchright backend + WireGuard helper (2026-05-02)

### Added

- **`patchright` auto-detected as Playwright backend** — When `patchright` is installed (`pip install patchright && patchright install chromium`), the Playwright bypass pass uses it automatically instead of vanilla Playwright. Patchright applies `rebrowser-patches` which fix the `Runtime.Enable` CDP leak and other automation signatures CF detects in vanilla Playwright. Vanilla Playwright is the fallback if patchright isn't installed.
- **`wireguard-setup.sh`** — Helper script that installs WireGuard and brings up a tunnel from a provider config. Use this when CF has IP-banned the server: route all traffic through Mullvad/ProtonVPN/IVPN's residential exits, then re-run the bot.

### Changed

- **Final CF-blocked error now lists 4 ordered fixes** — patchright (cheapest), VPN (next), residential proxy, run locally.

## v1.7.10 — Patch nodriver CDP cookie schema mismatch (2026-05-02)

### Fixed

- **nodriver hung indefinitely on `cookies.get_all()`** — nodriver's CDP cookie parser is hardcoded against an older Chromium schema and crashes with `KeyError: 'sameParty'` when newer Chromium omits that field. The crash happened in a background CDP listener task, leaving the foreground cookie call awaiting a response that never came. Now monkey-patches `nodriver.cdp.network.Cookie.from_json` to default the missing fields (`sameParty`, `sameSite`, `priority`, `sourceScheme`, `sourcePort`) before parsing, so cookie reads succeed against any Chromium build.

## v1.7.9 — nodriver hang protection + actionable CF block message (2026-05-02)

### Fixed

- **nodriver could hang the wizard indefinitely** — `uc.start()`, `browser.get()`, and `browser.cookies.get_all()` had no timeouts and could block forever if Chrome failed to launch or CDP became unresponsive. Now wrapped with `asyncio.wait_for` (20s start, 30s nav, 5s per cookie poll) plus a hard 120s outer timeout.
- **"Cloudflare blocked" message wasn't actionable** — When all 6 bypass passes fail (which means CF has IP-banned the server), the error now explicitly says no code change can fix it and gives the two real solutions: run from a residential IP, or set a residential proxy in the wizard.

## v1.7.8 — Use Playwright API to locate Chromium for nodriver (2026-05-02)

### Fixed

- **`_find_chrome_binary` filesystem search missed Playwright's Chromium** — Replaced with `playwright.chromium.executable_path` API call which always returns the correct path. Filesystem search kept as last-resort fallback, now also covers `PLAYWRIGHT_BROWSERS_PATH` env var, `/usr/local/share/ms-playwright`, and the newer `chrome-linux64/` and `chromium_headless_shell-*` directory layouts.

## v1.7.7 — nodriver auto-detects Chromium binary (2026-05-02)

### Fixed

- **nodriver `FileNotFoundError`** — nodriver defaults to looking for system Google Chrome and fails on servers without it. Now searches in order: `/usr/bin/google-chrome*`, `/usr/bin/chromium*`, `/snap/bin/chromium`, then falls back to Playwright's bundled Chromium at `~/.cache/ms-playwright/chromium-*/chrome-linux/chrome`. So if Playwright is installed, nodriver works automatically without any extra Chrome install.

## v1.7.6 — nodriver Pass 6 + Playwright Turnstile click (2026-05-02)

### Added

- **`nodriver` Pass 6** — Last-resort CF bypass using `nodriver` (the maintained successor to `undetected-chromedriver`). It uses raw Chrome DevTools Protocol so it doesn't carry Playwright's automation signatures. Triggered when Playwright Pass 5 also fails.
- **Playwright Turnstile click** — If the passive 15s wait doesn't yield `cf_clearance`, the bot now scans frames for the Turnstile iframe and clicks the verification checkbox before extending the wait by 30s.
- **Diagnostic on Playwright timeout** — Page title and body preview are printed/logged so you can see what CF is actually showing (still a challenge page vs. an outright block).
- **`--headless=new`** — Switched to Chrome's new headless mode which is significantly harder to detect than the legacy headless mode.

### Changed

- **Removed `playwright-stealth`** — Its 2.x API broke compatibility; replaced with inline `add_init_script` patches.

## v1.7.5 — Playwright error visibility + inline stealth (2026-05-02)

### Fixed

- **Playwright failures invisible** — Errors were logged at debug level and silently swallowed. Now surfaced to console (CLI) and logs (TG) so you can see exactly why Chromium launch / page navigation / cf_clearance wait is failing.
- **`playwright-stealth` 2.x API mismatch** — The dependency in 2.0.3 changed its API and `stealth_sync`/`stealth_async` no longer exist. Replaced with inline `add_init_script` containing equivalent anti-detection patches (webdriver flag, plugins, languages, chrome.runtime, permissions API). Removes the `playwright-stealth` runtime dependency entirely.
- **Cf_clearance wait window too short** — Bumped from 30s to 45s so slow Turnstile challenges have time to resolve.

## v1.7.4 — Playwright CF bypass for Turnstile/managed challenges (2026-05-02)

### Added

- **Playwright CF bypass** — New Pass 5 in the CF bypass chain uses a headless Chromium browser to solve Cloudflare Turnstile and managed challenges that FlareSolverr v1 cannot handle. Triggered automatically when all other passes fail. Requires `playwright install chromium` after update.
- **`playwright-stealth` support** — If `playwright-stealth` is installed, pages are patched to avoid headless detection.

### Fixed

- **`_solve_cloudflare` false positive** — Previously returned True when FlareSolverr got other CF cookies (`__cf_bm`) but not `cf_clearance`. Now only returns True when `cf_clearance` is present, preventing a failed retry from being silently skipped.
- **Stale CF cookies after bypass** — HTTP session is now recreated after FlareSolverr/Playwright solve to clear `__cf_bm` cookies accumulated during failed attempts.

## v1.7.3 — Cloudflare bypass improvements (2026-05-02)

### Fixed

- **Connection test used zero-amount bet** — Replaced with a lightweight GraphQL `AuthCheck` query (`user { id }`). Zero-amount bets to the casino endpoint trigger CF managed challenges; the GraphQL endpoint is lighter.
- **CF bypass only tried one curl_cffi profile** — Added profile rotation (chrome120 → chrome116 → chrome110 → firefox → edge) before falling through to FlareSolverr. Different TLS fingerprints succeed against different CF rule sets.
- **CF error message not actionable** — When all bypass attempts fail, the error now shows: `Cloudflare blocked — use /set proxy or /set cookie cf_clearance=VALUE`.
- **`_recreate_http` always used "chrome" profile** — Now accepts `impersonate` parameter so profile rotation can drive it.

## v1.7.2 — Cloudflare 403 retry during active betting (2026-05-02)

### Fixed

- **CF 403 kills session mid-run** — When Cloudflare re-challenges during active betting, the session now automatically re-authenticates (new HTTP client → cached cookies → FlareSolverr) and retries the blocked bet once, instead of stopping the session. Handles CF cookie rotation without user intervention.

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
