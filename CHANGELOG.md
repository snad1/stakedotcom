# Changelog

## CLI v1.20.0 / stakectl v2.7 - every lane visible and started with the bot, rolling restart (2026-11-26)

- `stakectl lanes` lists a registry lane that never ran (no state file yet) as `NOT RUNNING`, so a lane that should bet but doesn't is still seen; a broken state file still reads `unreadable`.
- `stakectl start 1` and `restart 1` also start the enabled lanes that are not running; running lanes are left alone, and `stop 1` touches no other lane.
- `stakectl restart --rolling` asks every running lane to restart on its saved config at its own session end; a stopped lane is named with `start it with: stakectl start K`, and with the bot down it says to start it (exit 1). No unit restarts now.
- `stakectl update` output is unchanged.

## stakectl v2.6 - removed lanes leave nothing behind (2026-11-26)

- `stakectl lane remove K` deletes lane K's state, pid, run and session-end files once its unit stops. Its history stays in the database, and a remove the bot refuses keeps them.
- `lane remove K --after-session` on a stopped lane removes it now (`Lane K is not running, so it is removed now.`): a stopped lane never reaches its session end.
- `restart K --at-boundary` on a stopped lane answers `Lane K is stopped; start it with: stakectl start K` (exit 1). `--cancel-boundary` still works on it.
- `stakectl reset` moves the database's `-wal` and `-shm` with it to the archive name, so the new database never reads the old log, and drops every session-end request.

## CLI v1.19.0 / stakectl v2.5 - what a change costs, restarts at a session end, rolling updates (2026-11-25)

Ports WolfBet's restart classes, pending restarts, session-end restarts and rolling update (WolfBet v2.55.0, v2.56.0, v2.58.0 to v2.60.0), the same code as shuffle 1.16.0. Stake has no events feed, so that part is N/A.

### Restart classes
- Every setting is classed live (a running lane re-reads it), lane (it applies when that lane restarts) or service (shared, every lane restarts). `config-set mult=3` prints `Restart every lane to apply: multiplier_target (stakectl restart).`; `lane2.loss_mult=3` prints `Restart lane 2 to apply: loss_mult (stakectl restart 2).`; live changes print no restart line. No setting changes how it applies.

### What a lane waits on
- A running lane lists the saved changes it has not picked up: `stakectl lanes` shows `Lane 2 waits for a restart: multiplier_target 2.0 -> 3.0 (stakectl restart 2)`. The access token, lockdown token and cookie are never listed. The state file carries `pending_restart` and `code_version`.
- The lanes table has a Code column: `new`, `waiting for session end` or `old`.

### Session-end restarts and rolling updates
- `stakectl restart K --at-boundary` re-executes lane K in place when its session ends; `--cancel-boundary` drops the request; `stakectl lane remove K --after-session` removes lane K then. With recurring off the restart is dropped and the lane stops as usual. The request (`~/.stake_autobot.drain`, `.laneK`, 0600) is read once per session close, never per bet.
- `stakectl update` asks each running lane to restart on the new code at its session end; betting goes on. `stakectl update --critical` restarts every lane now, as before.

### Tests
- `tests/test_lane_restarts.py`; `tests/test_stakectl_lanes.py` (`restart K --at-boundary`, `lane remove K --after-session`, rolling and critical update). Tests that pinned the old restart note or the versions updated.

## CLI v1.18.0 / TG v1.14.0 / stakectl v2.4 - the login in the vault, a lane on its own account and IP (2026-11-25)

Ports WolfBet's key vault and per-lane accounts (WolfBet v2.51.0 to v2.53.0, v2.57.0), the same code as shuffle 1.15.0, for Stake's three secrets. Needs casino-shared 0.12.0 (the `account` vault kind); with an older one the CLI prints `casino-shared 0.12.0 or newer is needed: run stakectl update` and exits 1.

### The login in the vault
- When lane 1 starts with `--resume` or `--daemon`, it stores `access_token`, `lockdown_token` and `cookie` in the host vault as one account entry, `default` (`~/.casino-vault/stake/default.json`, 0600). Once it reads back equal, the three fields leave `~/.stake_autobot.json`; one 0600 copy stays at `.pre-vault`. Saving the config never writes them back. If the vault can't be used (`Key vault unavailable: <reason>` in the log), the config keeps its copy. Lanes 2+ only read. A `--preset` run takes the login from the vault too.
- `stakectl key list | add ID [--label L] [--kind account|proxy|telegram] | rotate ID [--kind K] | delete ID`. An account is read through three hidden prompts (access token, lockdown token, the whole cookie header) and goes to the bot on stdin, never in argv, output or the log. A key in use can't be deleted; rotating with the wrong kind is refused before anything is read.
- The Telegram bot reads its token from the vault (`telegram`) first, then `STAKE_TG_TOKEN`.

### A lane on its own account and IP
- A lane may set `key_ref` (a vault account), `currency`, `proxy` (no password) or `proxy_ref` (a vault proxy). `config-set` still refuses the three secrets and `token`, with or without a `laneK.` prefix. `currency` can now be set with `config-set` (restart to apply).
- Saving a lane's key, currency or proxy restarts only the running lanes it changes: `Lane 3 restarted: its key, currency or proxy changed.`
- A lane whose account or vault proxy is missing logs `Lane 3: key friend-a is not in the vault` and exits instead of betting from another account. `lane3.key_ref=nobody` is refused with `Invalid lane3 override: no key named nobody in the vault`; a lane proxy with a password is refused without being echoed.
- The budget and bet splits count only the enabled lanes on the same account and currency. A vault proxy is never written to the config file.

### Cloudflare per IP
- cf_clearance is bound to the IP, so each proxy has its own cookie cache and re-auth lock (`~/.stake_cf_cookies.<hash>.json`); lanes with no proxy keep the host cache, as before. FlareSolverr and Playwright solve through the lane's proxy (with its login); nodriver gets `--proxy-server` and is skipped for a proxy with a password, which it can't send. A live proxy change drops the old IP's cookies.

### Tests
- `tests/test_lane_accounts.py` (login moved in with one backup, broken vault keeps the copy, lanes 2+ write nothing, own login/currency/proxy, split per account, missing key or proxy stops the lane, vault proxy kept out of the file, refusals without echo, only changed lanes restart, key verbs, Cloudflare cache and solver per proxy, Telegram token). `tests/test_stakectl_lanes.py` (`key` verbs send the secrets on stdin only). `tests/conftest.py` points the vault at each test's tmp dir. Lane tests updated where they pinned currency and proxy as lane-forbidden or set the lane count directly for the split.

## CLI v1.17.0 / stakectl v2.3 - named lanes, one lane at a time (2026-11-25)

Ports WolfBet's lane registry and one-lane verbs (WolfBet v2.48.0 to v2.49.0, wolfctl 3.22 to 3.24), the same code as goated 1.15.0 and shuffle 1.14.0. Needs casino-shared 0.10.0; with an older one the CLI prints `casino-shared 0.10.0 or newer is needed: run stakectl update` and exits 1. Every lane still shares one account and one Cloudflare cookie cache.

### Lanes
- Lanes have an id and a name: `lanes: {split, max, next_id, items: {"<id>": {name, enabled, overrides}}}`. A config with `lanes.count` reads as lanes 1..count, as before; the first lane write keeps one copy at `~/.stake_autobot.json.pre-registry` (0600).
- `stakectl lane add NAME [--from K]` adds one lane (copying lane K's overrides) and starts only it. Ids are never reused.
- `stakectl lane remove K` stops and disables only that lane: `Lane K stopped. Other lanes keep betting.` Lane 1 holds the base config and can't be removed.
- `stakectl lane rename K NAME` (or `--config-set laneK.name=NAME`): the running lane reloads and logs `[L3 friend-A]`. The lanes table gains a Name column once a lane has its own name.
- `stakectl start|stop|restart K` acts on one lane alone; plain `start|stop|restart` still act on every lane.
- `stakectl lanes N` starts or stops only the difference and never restarts `stake`. With the `bet`/`both` split the running lanes reload their base bet share; `budget`/`both` print `Budget shares update when each lane restarts: stakectl restart K.`
- The owner limit `lanes.max` (default 10, up to 99): `stakectl lanes --max N` or `--config-set lanes_max=N`. Lane ids go up to 99.
- New bot flags: `--lane-ids [enabled|all]`, `--lane-add NAME [--lane-from K]`, `--lane-remove K`.

### Tests
- `tests/test_lane_registry.py` (old shape read, `lanes=N` round trip, one-time backup, rename reloads one lane, add/remove, refusals write nothing, limit, ids above 10, `--lane-ids`, Name column, run reset, reload takes the count, an older casino-shared).
- `tests/test_stakectl_lanes.py` (add starts only the new unit, remove disables only its lane, rename, one-lane stop/start/restart, unknown lanes reach no unit, bad arguments exit 2, `--max`).
- `tests/test_wolfbet_sync_e5_lanes.py`, `tests/test_wolfbet_sync_e4.py` and `tests/test_wolfbet_sync_e7.py` updated for the registry shape, ids up to 99 and the one-lane delta.

## stakectl v2.2 + installer v1.4 - wolfctl 3.12 operations (2026-11-23)

Mirrors wolfctl 3.12 and WolfBet's installer at CLI v2.37.2, as goatedctl 1.1 does. `stakectl flaresolverr`, `warp-setup.sh` and `wireguard-setup.sh` are unchanged. `stake.py` is unchanged; every new command calls a flag it already has.

### Lanes
- `stakectl lanes [N] [--split none|budget|bet|both]` shows or sets the lane count. Lanes 2..N run as `stake-lane@K` and are enabled for boot; lanes above N are stopped and disabled. With the bot stopped, the kept lanes are only boot-enabled, so nothing starts while lane 1 is down.
- `start`, `stop`, `restart`, `start-all`, `stop-all` and `update` act on every lane; `status` adds the per-lane table when there is more than one; `monitor K` attaches to lane K.
- The `stake-lane@.service` template is written by the installer, and by `stakectl` when it is missing.

### Statistics
- `stakectl reset [--yes]` moves `~/.stake_autobot.db` to `~/.stake_autobot-<timestamp>.db` (never deletes it), removes the live-state files and the run files (`~/.stake_run*.json`) so the next start begins at session 1, and leaves the config alone. A running bot and its lanes are stopped, not restarted.
- `stakectl archives` lists the archived DBs with session counts, date spans and sizes.
- `stakectl run-reset [--yes]` starts a new run total for the run-wide profit increment and restarts the lanes that were running.

### Health and upkeep
- `stakectl doctor [--fix] [--yes]` checks the unit file, whether the CLI unit is enabled for boot (`[WARN]` with `systemctl --user enable stake`), the venv, the `casino-shared` import, config mode 0600, DB fragmentation and `integrity_check`, the journal, disk space, daemon priority stakectl drift, and FlareSolverr as `[INFO]` only (container running, and reachable at `FLARESOLVERR_URL` or `http://localhost:8191/v1`; no fix). On virtualized hosts the CPU governor advice is `[SKIP]`. `--fix` applies only safe fixes (enable the unit, chmod 600 the config, bet-row cleanup, VACUUM, journal vacuum); privileged ones are printed, never run.
- `stakectl update` pulls with `--autostash` and lists what it stashed, keeps the pinned requirements install (stops before any restart if it fails), installs the ctl with `install -m 0755`, warns `stakectl was stale: X -> Y` when the repo copy is newer, migrates `Restart=always` to `on-failure` and adds `Nice=-5` + IO priority to the CLI unit, and restarts the web admin, the Telegram bot and the CLI with its lanes only when they were running.
- `stakectl refresh` reinstalls stakectl from the repo copy in the current folder and prints the version change (no pull, no pip, no restart). `STAKECTL_VERSION=2.2` is the value it and `update` compare.
- New pass-throughs: `version`, `config-show`, `config-set`, `cleanup [DAYS]`, `benchmark [N] [--lanes M] [--yes]`, `diagnose`, `last-bets [N]`, `preset NAME`.

### Installer
- The dead `../shared` copy is gone (casino-shared comes from the pinned requirements).
- The CLI unit runs at `Nice=-5` with best-effort IO priority; the lane template is written next to it.
- `systemctl --user enable stake` after `daemon-reload`, for boot only (no `--now`): a reboot brings the bot back, and a bot stopped by hand stays stopped.
- The installer used to write its own inline copy of an older stakectl through a heredoc, so a fresh server got a ctl without export, backtest or lanes until the first update. It now installs the repo's `stakectl` with `install -m 0755`. The FlareSolverr container start (loopback only, pinned `v3.5.2`) is unchanged. Banner text uses plain ASCII dashes.

N/A: `pipeline` (stake has no `pipeline_depth`).

**Tests**: `tests/test_wolfbet_sync_e7.py` (60, the ctl run under bash against a temp HOME with stubbed systemctl and python). **489 total passing.**

## CLI v1.16.0 - exit reason, session export and offline backtest (2026-11-23)

Mirrors WolfBet CLI v2.20.0 (export), v2.21.0 (backtest) and v2.37.1 (exit reason), as goated CLI v1.13.0 does.

### Why the bot stopped
- The exit summary has a `Stop reason` row: the final status, or `-` when there is none.
- The daemon logs `DAEMON EXIT  session=N  bets=B  profit=+P  reason=...` after the session is saved and before the last state write, so the saved `status` and the log agree and a stop is never a silent clean exit.

### Export
- `--export PATH [--export-session-id ID]` writes one session (default: the latest). A `.json` path gives `{"session": ..., "bets": [...]}`; anything else gives CSV with the session as `# key: value` lines over the bets table. Files are mode 0600.
- `E` in the TUI exports the current session (default `~/stake-session-<id>.csv`).
- `stakectl export PATH [SID]`.

### Backtest
- `--backtest --simulate N --seed S` bets virtually from stake's own 1% edge: win chance `99 / multiplier_target` percent, limbo draws a crash multiplier `0.99 / U`, dice rolls 0-100 against `dice_target`. The same seed gives the same result.
- `--backtest --replay-session ID` replays a stored session's win / loss sequence with the current strategy's bet sizes and multiplier.
- Config comes from `--preset NAME`, else the saved config, else Martingale limbo 2x. The run is in memory on a 1.0 sandbox balance: no API call, no DB write, no run file.
- The backtest never builds an HTTP client, solves Cloudflare or opens a browser: `load_config(rebuild_http=False)` skips the client rebuild that every other caller still gets (tested with the client, CF solvers and sockets patched to fail, and no browser module imported).
- The live loop's stats update and next-bet code moved into `_record_bet_outcome` and `_next_bet_after`; the live loop and the backtest both call them, so the maths cannot drift. Stop conditions use the same `should_stop`.
- `stakectl backtest [args]`.

N/A or not ported: WolfBet's Rich-panel session summary (the existing summary table carries the stop reason instead); recurring restarts inside a backtest (a stop ends it); WolfBet's later `--json` export report and multi-session backtest.

**Tests**: `tests/test_wolfbet_sync_e6.py` (26). **429 total passing.**

`VERSION` `1.15.2` -> `1.16.0`.

## CLI v1.15.2 - the base bet you set is the base bet it bets (2026-11-23)

Mirrors WolfBet CLI v2.37.2. A base saved while the bot ran never reached it, a restart re-inflated a new base by every bump earned on the old one, and `--config-show` could not show what the daemon was betting.

### A saved base reaches the running daemon
- `base_bet` is live over SIGHUP, so `--config-set basebet=...` and the wizard's save apply at once and no longer list it under "need a full restart".
- A base change drops the run's bumps, records the new base and re-anchors the next milestone one step above the run's current profit, so the next bump needs fresh profit. The current bet resets to the new base.
- A change is judged against this lane's configured base, not the live bumped one, so an unrelated reload (`--config-set maxloss=5`) keeps the bumps. WolfBet compares against the live base, which drops the bumps on any reload after a bump; that differs here on purpose.
- Only numbers change on a live base change: no CF re-auth and no client rebuild (tested).

### Bumps belong to the base they were earned on
- The run file stores `base`. On start, bumps earned on another base are dropped with a `RUN base changed` warning and the milestone re-anchors above run profit; on the same base they apply as before.
- A run file written before this version keeps its bumps once and adopts the current base.

### The screen shows the live value
- `--config-show` marks the `base_bet` row `0.00005805  (live 0.00006534, +729 bumps)` when a running daemon bets a different base.
- `stakectl start` on an active unit says `CLI bot was already running - nothing restarted.` with the `stakectl restart` hint.

N/A: WolfBet's auto-bet restart after a base change (no server-side auto-bet here). Known interaction, unchanged: with `basebet_pct > 0` the base is recomputed from the balance, so a hand-set base is transient there. No schema change; the Telegram bot is unchanged (it already keys bumps to their base).

`VERSION` `1.15.1` -> `1.15.2`.

## Security hotfixes: web, Telegram and limits (2026-11-15)

### Web admin
- `STAKE_WEB_SECRET_KEY` and `STAKE_WEB_ADMIN_PASS` must be non-default (32+ bytes, 12+ chars); the app exits before binding.
- The installer generates the secret, prompts for or generates the password, writes `.env` with `printf` at 0600, refuses a weak existing `.env`, and always binds uvicorn to `127.0.0.1`.
- Login lockout and single-use Telegram links are wired through the shared auth router.
- Every `/api/users/{user_id}/...` route uses `require_user_access`.
- `web/bot_db.py` exports the bound `_connect_ro(user_id)`, so the live session socket resolves.
- The admin user page renders this service's secret keys as `hidden`.

### Telegram
- `/update` answers only to `TG_OWNER_ID`, with no fallback id and a startup warning when it is missing; the launcher is an argv list for `systemd-run` with validated paths and a 0600 log under the data dir.
- `/set` and `/tweak` reject `nan` and `inf` for every numeric parameter, and the CLI config-set path uses the same check.

### Updates
- `stakectl update` installs the pinned requirements and stops when the install fails.

### Cloudflare surfaces
- FlareSolverr is pinned to `v3.5.2` and published on `127.0.0.1:8191` only; Docker comes from `apt-get install docker.io` instead of a piped install script.
- The Worker requires an `X-Proxy-Key` header matching its `PROXY_KEY` secret, and the CLI sends `STAKE_PROXY_KEY` when set.
- Telegram browser solves share one process-wide semaphore, Chromium keeps its sandbox unless the bot runs as root, and the cross-lane Cloudflare lock now times out instead of waiting forever.

**Version**: see the module constants; `casino-shared` pinned to the dashboard branch.

## CLI v1.15.0 + TG v1.13.0 — Run-wide profit increment (2026-11-15)

Mirrors WolfBet CLI v2.37.0 (`0b7e71a`) and TG v1.21.0 (`58d6e4d`), `profit_increment_scope` / `piscope`. Helpers come from `shared.core.run_increment` (casino-shared 0.3.0). Cloudflare chain, auth and the bet client are untouched: a bumped base bet only changes the amount of the next bet.

- **Scope.** `profit_increment_scope`: `session` (default, unchanged: this session's profit, milestone reset each recurring cycle) or `run` (the lane's sessions since the run began, plus the live session; DB sum + run file, so the milestone survives recurring cycles and restarts). CLI aliases `piscope` / `pscope`; wizard asks the scope after the increment prompts; `--config-set piscope=run` validates (exit 2) and says a restart is needed; `--config-show` shows it.
- **Run file.** `~/.stake_run.json` (`STAKE_RUN_PATH`), one per lane: `~/.stake_run.laneK.json` for lane K. Written atomically (0600) on each bump. A file pointing past the DB's last session (stats reset) starts a new run. Start: `base_bet = config base + bumps × increment`, next milestone `(bumps + 1) × threshold`. Recurring cycles attach the new session row to the same run.
- **Off values.** `profit_increment` / `profit_threshold` set to `none` / `off` / `0` / `0.0` (≤ 0) are fully off: no bumps, no background run; the run file is removed at start or on SIGHUP, and re-enabling starts a fresh run on the current session.
- **Merged check.** The inline increment blocks are one `_check_profit_increment()` (CLI) / `_maybe_profit_increment()` (TG).
- **Display.** CLI `--status` adds a `Run Profit` row (`since #N, bumps, next at X`) and the monitor shows `RunMs` instead of `Ms`; the state file carries `profit_increment_scope`, `run_profit_total`, `run_first_session`, `run_bumps`. TG `/config` and `/status` show the scope and run total; the web settings page shows the increment block.
- **Reset.** CLI `--run-reset [--lane K]`; TG `/runreset` refuses while sessions or recurring restarts are pending, then asks for confirmation before deleting the run file. `stakectl run-reset` comes with E7.
- **TG run sharing.** One run per user across every slot: a bump in one slot raises the others' base bet on their next refresh, and `_run_base_origin` keeps recurring restarts from stacking a bump twice. `/tweak piscope` is refused (the scope applies on the next session; use `/set piscope`) — a deviation from WolfBet, which has no live scope change either.
- **N/A:** the auto-bet UUID restart on a bump — Stake has no server-side auto-bet, so the bumped base applies to the next bet.
- Tests: `tests/test_wolfbet_sync_e10.py`. `test_portability_v1.py` pins 1.15.0.

`VERSION` `1.14.0` → `1.15.0`; TG `1.12.0` → `1.13.0`.

## CLI v1.14.0 — Lane split minimum bet (2026-11-15)

Completes WolfBet CLI v2.36.0 (`lane_split`, `6174b96`) with the stakedotcom adapt from backlog Epic 9. The rest of v2.36.0 shipped in 1.13.0. TG unchanged.

- **Minimum bet** — in `bet` / `both`, a split `base_bet` or `basebet_pct` bet below Stake's minimum (0.0001) now stays full, with a warning in the log. Before, it was divided and then raised to 0.0001 at bet time. Same on load and on SIGHUP reload; an untouched full bet is still saved as is. A lane's own `base_bet` override is never divided. Uses `lane_config(..., min_bet)` and `split_base_bet` from casino-shared 0.3.0.
- **No default change** — stakedotcom 1.13.0 already shipped `lane_split=none` as the default, so lanes never had the always-on v2.35.0 budget guard here. The budget stop applies only with `lane_split=budget` or `both`, as before.
- N/A: nothing new.
- Tests: `tests/test_wolfbet_sync_e9.py` (13). `test_portability_v1.py` pins 1.14.0.

`VERSION` `1.13.0` → `1.14.0`.

## CLI v1.13.0 — Parallel lanes (2026-11-14)

Mirrors WolfBet CLI v2.35.0 (parallel lanes) and v2.36.0 (`lane_split`); where the two differ, v2.36.0 code wins. Lane helpers come from `shared.core.lanes` (casino-shared 0.2.0). TG unchanged. Cloudflare bypass chain unchanged.

- **Lanes.** N independent bot processes on one account: `stake.py --daemon --resume --lane K` (1–10). Each lane has its own session, chain, counters, stops, pauses and recurring / streak-reset cycle. The config file, CF cookie cache and stats DB are shared. Lane 1 keeps the classic state / pid paths, systemd unit and config writes. Lane K > 1 uses `~/.stake_autobot_live.laneK.json` / `~/.stake_autobot.laneK.pid` and logs with a `[LK]` prefix. `--lane K` is applied before any command, so `--status` / `--monitor` / `--stop` / `--diagnose` act on that lane. Lane K's first bet waits `(K−1)·2 s`.
- **DB.** New `sessions` rows carry `lane`.
- **Money split** (`lanes.split`, `--config-set lane_split=MODE`):
  - `none` (default): full base bet, no lane budget.
  - `budget`: each lane may risk balance ÷ lanes.
  - `bet`: base bet and `basebet_pct` ÷ lanes. A lane's own `base_bet` override is never divided; a split that rounds to 0 stays full.
  - `both`: `bet` and `budget` together.

  With a budget, the share is fixed at lane start from the account balance (GraphQL `UserBalances`; a `--preset` run fetches it when a budget is on). Stake's bet reply has no balance, so lane equity = share + the lane's profit across its sessions. The lane stops with `Lane budget exceeded (next bet …, lane equity …)` when the next bet is larger than its equity. That stop never restarts. The streak-reset chain-depth warning uses the lane share in `budget` / `both`. `save_config` writes the full base bet back when the split one is untouched.
- **Per-lane overrides.** `--config-set lanes=N`, `laneK.key=value` and `laneK.key=default` (also `unset` / `inherit`) write `lanes.count` / `lanes.overrides`. Values go through the normal `--config-set` validation (aliases; `mult` re-derives the dice target). Shared-only keys are refused per lane (exit 2): `access_token`, `lockdown_token`, `cookie`, `currency`, `game`, `proxy`, `purge_days`, `db_write_mode`. The Cloudflare worker / WARP / solver settings are CLI flags and env vars (`--api-base`, `STAKE_API_BASE`, `FLARESOLVERR_URL`), not config keys, so every lane shares them. `save_config` keeps the `lanes` block; a lane ≥ 2 never writes the base config; SIGHUP reload merges the lane's overrides. A `laneK.*` change SIGHUPs that lane; a base `--config-set` SIGHUPs every running lane. `--config-show` prints the lane count, split mode and overrides. A `--preset` run uses the saved lane count and split, but no lane overrides.
- **`--lanes-status`.** One row per lane state file (state, session, uptime, run, profit, lane equity, bps), plus total bps and P/L.
- **`--benchmark [N] --lanes M`.** Runs the read-only GraphQL `UserBalances` probe at 1..M concurrent clients. Each client has its own curl_cffi session with the cached CF cookies and the proxy. It prints req/s, scaling vs 1 lane, p50, ok, 429 and 403 per row, then `recommend_lanes`: the highest count where each step adds ≥ 30 % req/s with no 429. No bets, and no CF solvers.
- **Shared Cloudflare cookie cache.** `~/.stake_cf_cookies.json` is now written atomically (tmp file + `os.replace`, mode 0600), so a lane never reads a half-written file. CF passes 2–6 of the connection test (cache, TLS profiles, FlareSolverr, Playwright, nodriver) now run as `_cf_reauth`, unchanged, under an `fcntl` lock on `~/.stake_cf_cookies.json.lock`. Only one lane solves the challenge. A lane that waited for the lock re-reads the cache in pass 2 and reuses the first lane's cookies. The lock covers start-up re-auth only: a 403 during betting still only backs off, as before.
- **Monitor `S` / systemd.** The stop uses this lane's unit (`stake` or `stake-lane@K`), so systemd does not respawn a managed lane. WolfBet checks the lane-1 unit only; this is a small deviation, same as goated.
- **N/A:**
  - Soft pacing from quota headers: Stake sends none, so each lane keeps its 429 backoff.
  - The cross-lane `/auto/start` lock: Stake has no server-side auto-bet.
  - `pipeline_depth` as a forbidden key: no pipeline.

  `stakectl lanes` and the `stake-lane@.service` template come with E7.
- Tests: `tests/test_wolfbet_sync_e5_lanes.py` (61). They include two lanes in threads on one fake account, and two lanes hitting a CF 403 together with one re-auth. `test_portability_v1.py` pins 1.13.0.

`VERSION` `1.12.0` → `1.13.0`.

## CLI v1.12.0 — Observability, diagnostics, retention, async writer, live editing (2026-11-14)

Mirrors WolfBet CLI v2.17.0 (API timing + on-target %), v2.18.0 (wizard Step 5b + preset keys), v2.19.0 (`T` tweak / `_apply_mutation`), v2.22.1 (risky-config hints), v2.25.0 (`--config-show` / `--config-set` / `--version`), v2.26.0 (proxy, `purge_days`, `--cleanup`), v2.26.1 (`--benchmark`, `--diagnose`), v2.27.0 (async bet writer), v2.28.1 (monitor footer hints, systemd-aware stop), v2.28.3 (config-set → SIGHUP), v2.30.3–v2.30.6 (windowed metrics, clamp messages) and v2.32.1 (`martingale_chain_depth` warning). TG unchanged. Cloudflare bypass chain unchanged. Uses `shared.core.diagnostics`, `shared.core.net` and `shared.core.database`.

- **Metrics.** Each bet HTTP round trip is timed (a non-200 reply counts too); each loop iteration is a cycle. TUI adds `API last / p50 / p99 / avg / peak`, `API stable … on-target` (with a bet delay), `Efficiency` and `Cycle … (overhead) -> bps`. `--status` adds API / Cycle / Efficiency. The state file adds `api_*`, `cycle_*`, `bets_per_second_recent`, `efficiency_pct`, `theoretical_max_bps`, `db_write_mode`, `db_writer_stats`, `db_writer_queue`, `purge_days`. Windows hold 500 samples; Recent BPS and the ceiling both use means.
- **`--benchmark [N]`** sends N (3–200, default 20) GraphQL `UserBalances` queries through the live client (same headers, cached CF cookies, proxy). No bets. Prints min / p50 / mean / p99 / max, the p50 BPS ceiling and the shared verdict ("Stake is throttling…" on 429). It never runs the CF solvers: a 403 is a failed sample with a hint. `--proxy` overrides the saved proxy for the run.
- **`--diagnose`** reads the state file: actual / recent / theoretical BPS, efficiency, cycle median / p99 and breakdown, DB-writer stats, top blocker and advice.
- **Retention.** `purge_days` (default 3, clamped 1–30) in config, presets, SIGHUP keys and tweak (`purgedays`). `--cleanup [DAYS]` purges bet rows of sessions ended more than DAYS ago (`cleanup_old_bets`; session rows kept). The bot purges once at start and hourly while running (`cleanup_live_bets`).
- **Fix: saved proxy ignored on `--resume` / `--daemon` / `--preset`.** `load_config` now rebuilds the HTTP client, so the saved proxy applies (it used to apply only after a CF profile rotation). `--preset` takes the saved proxy from the config file. `--proxy` wins over the saved one. Proxies are validated (`http` / `https` / `socks5`; `off` = direct; a bad saved proxy is dropped with a log line) and masked in output. Without curl_cffi a set proxy is a no-op: the CLI now says so instead of going direct silently.
- **Async bet writer.** `db_write_mode` `async` (default) / `sync`. Async queues rows (max 1000) for a writer thread that commits batches of up to 32 within 0.1 s; a full queue falls back to a sync write; the queue drains on SIGTERM / quit / daemon exit. A live flip only reroutes new rows.
- **Live editing.** `T` in the TUI opens the tweak prompt (`param value`, aliases like `delay`, `maxprofit`, `bbpct`, `srloss`, `recdelay`, `mult`, `dbmode`). `_apply_mutation` validates and clamps with `⚠` lines; a new base bet or multiplier applies to the next bet; a dice multiplier re-derives the target with the wizard's formula. `--config-show` prints the saved config (tokens, cookie and proxy masked). `--config-set KEY=VALUE …` saves, prints old → new and clamp warnings, names the keys that need a restart and SIGHUPs a running daemon; credentials are refused (exit 2).
- **Wizard.** New Step 6b (optional: recurring, delays, streak reset, `basebet_pct`, streak pairs, milestones, `purge_days`, `db_write_mode`) and Step 7 (save preset: `save_preset` was never reachable before). Step 8 shows risky-config hints: aggressive Martingale, no `max_loss` on Martingale (`2`) / Delay Martingale (`6`), the chain-depth warning with a streak reset, Anti-Martingale with no `max_profit`, Rule-Based (`7`) with no `max_bets`, base bet > 1 % of balance, multiplier > 10, recurring with no cap, `basebet_pct` ≥ 5 %, `http://` proxy. The banner shows `VERSION` (was "v1.2").
- **Monitor.** Footer adds `--config-set KEY=VALUE` and `--stats / --analytics` hints. `[S]top` uses `systemctl --user stop stake` when the daemon runs in the `stake` unit (so `Restart=` does not respawn it), else SIGTERM.
- **N/A:** rate-limit row and rate-limit advice (Stake sends no rate-limit headers); pipelining and its warnings / advice; auto-UUID clearing on tweak (no server auto-bet); max-nonce recovery. Presets keep excluding `proxy` (WolfBet includes it): a preset must not change the network path the CF cookies were solved on. **Later:** `--benchmark --lanes` and lane runtime, `stakectl` pass-throughs for the new commands.
- Tests: `tests/test_wolfbet_sync_e5.py` (92); `test_portability_v1.py` version pin.

`VERSION` `1.11.0` → `1.12.0`.

## CLI v1.11.0 — Stats windows, durations, analytics, lane filter (2026-11-14)

Mirrors WolfBet CLI v2.23.0 (`--analytics`), v2.30.9 / v2.31.0 (date-windowed stats and analytics), v2.33.0 (time-of-day window fix), v2.33.1 (wins / losses per session), v2.34.0 (durations) and v2.35.0 (lane filter, "By lane"). TG unchanged. Uses `shared.core.stats` and `shared.core.lanes`.

- **Fix: time-of-day window.** `--since "2026-11-13 14:00"` matched the whole day (`' ' < 'T'`). The window now comes from `shared.core.stats.stats_date_window`: the space becomes `T`, a bad bound prints `Invalid --since/--until "…": use YYYY-MM-DD[ HH:MM[:SS]]` and no rows, and a bare `--until` date runs to `T23:59:59.999999`.
- **`--stats` totals** add `Sessions: N  (first -> last)`, `Time Span … Bet Time …` (span includes pauses between sessions; bet time sums each session, a running one ends now), `Wins/Session: Max N Avg N.N` and `Losses/Session: Max N Avg N.N`, all window-aware. "No database found" now shows when there is no DB (the DB used to be created first).
- **New `--analytics [--since --until --lane]`**: Overall panel (sessions in profit / loss, bets, win rate, wagered, P/L, best / worst session, `Time range` / `Time span` / `Bet time`), By game (Limbo / Dice), By strategy, Top 5 best / worst sessions. `stakectl analytics` passes through.
- **Lanes (v2.35.0).** `sessions.lane` is added to older DBs by `init_db` (`ensure_sessions_lane`; old rows read as lane 1). `--stats` / `--analytics --lane K` filter to one lane (1-10, else exit 2); session headers show `[LK]` for lane > 1; analytics shows "By lane" when more than one lane has sessions. Running lanes arrived in a later release, so until then every row is lane 1.
- `Run …` in the TUI / monitor headers and `--status` "Running for" already shipped in 1.10.0.
- **N/A:** none. **Later:** lane runtime, `stakectl lanes` / `--lane` pass-through.
- Tests: `tests/test_wolfbet_sync_e4.py` (19); `test_portability_v1.py` window and version pins updated.

`VERSION` `1.10.0` → `1.11.0`.

## CLI v1.10.0 — Recurring and streak-reset cycles, live reload (2026-11-14)

Mirrors WolfBet CLI v2.16.0 (recurring, streak delays, `streakbet_loss`, `basebet_pct`, milestones), v2.25.3 / v2.30.6 (`recurring_delay` 0..86400 with a clamp message), v2.28.2 (config persistence), v2.28.3 (SIGHUP live reload), v2.28.4 (uptime freezes in the pause), v2.30.1 / v2.30.7 (`next_profit_milestone` persisted, `Ms` per-window progress), v2.32.0 (streak reset) and v2.32.1 (insufficient balance terminal). TG unchanged. Cloudflare bypass chain and its 403 handling unchanged. Uses `shared.core.session_rules`.

- **Cycles.** With `recurring` on, a profit-target / max-bets / max-wins stop at profit ≥ 0 closes the `sessions` row, pauses `recurring_delay` (default 60 s) and opens a new row. `streak_reset_losses` N stops on the first win after N+ losses (profit ≥ 0) and restarts after `streak_reset_delay` (default 600 s), even with `recurring` off. Each new cycle zeroes counters and streaks, keeps the bumped `base_bet` and re-arms `next_profit_milestone` from `profit_threshold`. `[Q]` / SIGTERM during the pause stops without opening a new row.
- **Balance at cycle start.** Stake returns no balance in the bet reply, so the CLI tracks it locally. Each new cycle reads the real balance once via the GraphQL `UserBalances` call; if that fails, the local balance is kept.
- **Loop hooks.** `streak_delay_loss/win/bets` ("N:sec") sleep before the next bet; `streakbet_loss` ("N:mult") scales the next bet every N losses; `basebet_pct` re-sizes the base bet from the balance every 10 bets; `milestone_bets/wins/losses/profit` log and show `Milestone: …`.
- **Uptime.** During a cycle pause `uptime_sec` freezes at the last bet (and the session's `ended_at` uses it); the state file adds `pause_remaining_sec`, `pause_total_sec` and `run_uptime_sec` (never resets across cycles). Monitor and TUI headers show `◐ PAUSED x/y` and `Run …`; `--status` shows `Recurring pause: X remaining / Y total`, `Running for`, `Recurring` and `Streak Reset`.
- **CFG row.** `Ms` reads `next_profit_milestone` (it read `last_profit_milestone`, which the CLI never set) and shows progress inside the current window; `Rec on(Xs)` now appears (the state file writes `recurring`); new `SR nL→Ss` segment.
- **Persistence + reload.** Every new key is in the config file and presets (streak pairs stored as `"N:X"`, `null` when off; delays clamped on load). No wizard prompts yet (wizard Step 5b is E5): set the keys in the config file or a preset, then SIGHUP. `SIGHUP` re-reads the config and applies only live-safe keys (stops, delays, cycle keys, streak pairs, milestones, `profit_increment` / `profit_threshold`, `custom_rules_text`, `proxy`); other changed keys are logged as skipped. SIGUSR1 / SIGUSR2 pause / resume unchanged.
- **Fix: bet error envelope.** An HTTP-200 `{"errors": [...]}` bet reply was booked as a 0-amount loss; it is now an error (backoff, no bet row). Insufficient balance matches "insufficient" only (the `ConnectionError` path matched any message containing "balance"). Insufficient balance is terminal: no recurring or streak-reset restart.
- **Fix: parsers** fall back to `state.result` when the reply has no top-level `resultMultiplier` / `resultTarget` / `result`.
- **N/A:** server-side auto-bet and its auto-UUID clearing, the mid-chain auto guard, `--pipeline` — Stake bets are one request each. **Later:** chain-depth risky-config warning and `config-set`, lanes.
- Tests: `tests/test_wolfbet_sync_e3.py` (52); `test_portability_v1.py` version pin.

`VERSION` `1.9.1` → `1.10.0`.

## TG v1.12.0 — Streak reset, delay caps, analytics window (2026-11-14)

Mirrors WolfBet TG 1.20.0: v2.30.6 (delay cap), v2.32.0 (streak reset), v2.32.1 (insufficient balance terminal), v2.33.0 (time-of-day window). CLI unchanged. Cloudflare bypass chain unchanged.

- **Streak reset** — `/set srloss N` (alias `streakreset`, `off` to disable) stops the session on the first win after N+ consecutive losses, only while profit ≥ 0. Stake has no balance in the bet reply, so the check uses the engine's locally tracked profit. The bot then starts a new session (new `sessions` row) after `/set srdelay S` (alias `streakresetdelay`, default 600 s, 5–86400), **even when `recurring` is off**. Live `/set srloss|srdelay` carries into the next cycle; the bumped `base_bet` is kept and `next_profit_milestone` re-arms from `profit_threshold`. Uses `shared.core.session_rules` (`record_outcome`, `streak_reset_triggered`, `STREAK_RESET_REASON`).
- **Cloudflare across a restart** — a streak-reset restart goes through the existing `_recurring_restart` path: the new engine's `start()` tries the direct AuthCheck, then the cached `cf_cookies.json`, before any solver, so it does not force a new CF solve. A CF 403 during the next cycle still goes through `_cf_reauth`.
- **Delay caps** — `/set recdelay` and `/set srdelay` clamp to 5..86400 and tell you when they clamp. A saved `recurring_delay` above 24 h is capped at the restart.
- **`/tweak`** gains `srloss` (live on the running engine) and `srdelay` / `recdelay` (saved; apply at the next stop).
- **`/config`, `/status`, stop message** show `Streak reset … / SR nL→Ss` when set; `/help` lists the keys; web settings page lists `streak_reset_losses` / `streak_reset_delay`; both keys are in `CONFIG_KEYS` (presets) and the `/resume` snapshot.
- **`/analytics`** builds its window with `shared.core.stats.stats_date_window`: `/analytics 2026-11-13 14:00` now starts at 14:00 instead of matching the whole day (`' ' < 'T'`). A time token after a date joins it, since Telegram splits on spaces. Bad bounds print the usage.
- **Fix: insufficient-balance match.** The bet `errors` path and the `ConnectionError` path (HTTP 400) stopped the session on any message containing "balance" (`"insufficient" or "balance"`), the same bug as goated and shuffle. They now match "insufficient" only. Insufficient balance stays terminal: no recurring or streak-reset restart.
- Tests: `tests/test_wolfbet_sync_e2.py` (32); fake gains `api_error_balance` (HTTP 200 `errors`) and `http_error_balance` (HTTP 400). `test_portability_v1.py` version pin and the `/analytics` source check updated for the shared helper.

`tg/__init__.py` `1.11.1` → `1.12.0`.

## CLI v1.9.1 / TG v1.11.1 — casino-shared over HTTPS (2026-11-13)

Mirrors wolfbet v2.16.11 (casino-shared dependency installs over HTTPS). No bot behaviour change.

- **`requirements.txt`, `web/requirements.txt`: `casino-shared` via `git+https://github.com/snad1/casino-shared.git` instead of `git+ssh://`.** The repo is public, so pip fetches it anonymously; fresh servers no longer need a GitHub SSH key to run `install.sh`.
- **`stakectl update`** reinstalls casino-shared from the same HTTPS URL (it used `git+ssh` too).
- **Pinned to casino-shared `v0.2.0`** (`@v0.2.0`): the WolfBet sync helpers (`shared.core` session rules, stats, diagnostics, net, lanes and `sessions.lane`). `stakectl update` still reinstalls the latest `main`, as `wolfctl` does.

`VERSION` `1.9.0` → `1.9.1`. `tg/__init__.py` `1.11.0` → `1.11.1`.

## CLI v1.9.0 / TG v1.11.0 — Date-range stats + analytics (2026-08-15)

Mirrors wolfbet v2.30.9 / TG v1.19.0. No bet-loop change. Cloudflare Worker proxy, warp-setup, wireguard-setup, and `web/` all untouched.

- **CLI `--stats --since/--until DATE`** — new `_stats_date_window()` helper builds an optional `WHERE started_at` clause over the sessions table. Bare dates (`YYYY-MM-DD`) or full ISO timestamps; a bare `--until` is extended to end-of-day so the day is inclusive. Both the session listing and the totals query honor the window; headers show the window and switch to `Range Totals`. Filtered runs bump the listing limit from 20 to 50. `--since`/`--until` sit on the standalone parser (never the mutually-exclusive command group), so `--stats --since 2026-08-14` parses cleanly.
- **`stakectl stats`** — now forwards extra args (`stakectl stats --since 2026-08-01 --until 2026-08-15`); usage line updated.
- **TG `/analytics [SINCE] [UNTIL]`** — optional date window across all five analytics queries (lifetime, by-game, by-strategy, top-5 best, top-5 worst). Header shows `Range analytics` + window when filtered, `Lifetime analytics` otherwise. Usage hint on >2 args; `/help` documents the date form.
- **Tests** — `tests/test_portability_v1.py` grows 7 date-window tests (helper unit cases, seeded-DB functional filtering incl. inclusive bare `--until`, no-filter regression, argparse-placement source check, TG source check, stakectl pass-through). Version assertions bumped.

`VERSION` `1.8.0` → `1.9.0`. `tg/__init__.py` `1.10.0` → `1.11.0`.

## CLI v1.8.0 / TG v1.10.0 — WolfBet hardening subset ported (2026-07-19)

Same portable subset as the other 4 sibling bots (cryptogames, duckdice, shuffle, goated). No bet-loop change. Cloudflare Worker proxy, warp-setup, wireguard-setup, and `web/` all untouched.

- **SQLite PRAGMAs** on both CLI `_db_conn` and TG `_get_conn` (synchronous=NORMAL, busy_timeout=5000, cache_size=-32000, temp_store=MEMORY on top of the existing `journal_mode=WAL`). Mirrors wolfbet v2.27.0. 30–40 ms per-commit fsync savings on SSD; WAL-safe.
- **`Restart=on-failure`** in `install.sh` for both the daemon and TG units (was `Restart=always`). Mirrors wolfbet v2.28.1.
- **Uptime pause bookkeeping** — new `session_bet_ended_at` + `session_pause_started_at` fields on `BettingEngine`. `pause()` freezes, `resume()` clears. `get_status()` uses frozen bet-end for uptime. `_db_save_session(final=True)` writes frozen `ended_at` during pauses. Emits `uptime_sec` + `pause_remaining_sec` on the status dict. Mirrors wolfbet v2.28.4.
- **Live monitor** — sparkline preserved (dice + limbo history unchanged); config-progress row `CFG …` added immediately after. `bets_avail` budget shifts from `term_h - 10` to `term_h - 11`.
- **State-file monitor** — old placeholder `" P/L —"` replaced by the same config-progress row.
- Shared `_build_config_progress_row(d)` + `_pct_color()`: `Ms` / `MaxP` / `MaxL` / `MaxB` / `MaxW` / `MinBal` / `Rec` progress segments. Green <80%, yellow ≥80%, bold_red ≥95%. Fallback `CFG — no goals set` when nothing armed. Mirrors wolfbet v2.30.0.
- **TG `/diagnose`** — top-blocker analysis + advice, or `not enough data` fallback.
- **TG `/analytics`** — lifetime + by-game + by-strategy + top-5 best/worst.
- **First test suite** — `tests/test_portability_v1.py` — 21 tests covering all of the above.

`VERSION` `1.7.0` → `1.8.0`. `tg/__init__.py` `1.9.10` → `1.10.0`. `stakectl` unchanged.

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
