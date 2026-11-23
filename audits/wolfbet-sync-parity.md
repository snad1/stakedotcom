# WolfBet sync parity: Stake

What this repo does compared to WolfBet CLI 2.16.0 to 2.37.2 and WolfBet TG v1.21.0, as of Stake CLI 1.16.0 and TG 1.13.1 (2026-11-23).

- **Ported**: same behaviour as WolfBet.
- **Adapted**: same intent, changed to fit this casino's API (the note says how).
- **N/A**: the casino API cannot support it; the note gives the API reason.

Paths are relative to this repo. Run the tests with `APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests -q` from the workspace root.

| WolfBet | Change | Status | Where | Test | Note |
|---|---|---|---|---|---|
| 2.16.0 | recurring restart, streak delays, `streakbet_loss`, `basebet_pct`, milestones | Ported | `stake.py:2524`, `stake.py:2476`, `stake.py:2495`, `tg/engine.py:179` | `tests/test_wolfbet_sync_e10.py::test_session_scope_keeps_milestone_reset_in_recurring` | TG already had it |
| 2.16.0 | 429 Retry-After backoff | Ported | `stake.py:2203` | `tests/test_wolfbet_sync_e0.py::test_cli_recovers_from_429_and_timeout` |  |
| 2.17.0 | API last / avg / peak / p50, on-target %, cycle, efficiency | Ported | `stake.py:1533`, `stake.py:1546`, `stake.py:1593` | `tests/test_wolfbet_sync_e5.py::test_on_target_pct` | no rate-limit row: N/A, the API returns no quota headers |
| 2.18.0 | wizard Step 5b + preset keys | Ported | `stake.py:4546` | `tests/test_wolfbet_sync_e3.py::test_new_keys_in_config_and_preset_lists` |  |
| 2.19.0 | `T` tweak hotkey, `_apply_mutation` | Ported | `stake.py:1772`, `stake.py:3590`, `tg/handlers.py:1330` | `tests/test_wolfbet_sync_e5.py::test_show_tweak_applies_and_restores_pause` | no UUID clear: no server auto-bet; TG already had `/tweak` |
| 2.20.0 | exit summary, CSV / JSON export, `E` hotkey | Ported | `stake.py:4672`, `stake.py:4747`, `stake.py:3563` | `tests/test_wolfbet_sync_e6.py::test_export_csv_shape_and_mode` |  |
| 2.21.0 | backtest / replay | Adapted | `stake.py:4774`, `stake.py:4858`, `stake.py:4794` | `tests/test_wolfbet_sync_e6.py::test_backtest_is_deterministic_by_seed` | service house edge and payout |
| 2.22.0 | `core/games.py` WolfBet game registry | N/A | - | - | WolfBet API game list; each service keeps its own game builders |
| 2.22.1 | `_check_risky_config` | Ported | `stake.py:1646` | `tests/test_wolfbet_sync_e5.py::test_risky_clean_config` |  |
| 2.23.0 | `--analytics` | Ported | `stake.py:6124`, `tg/handlers.py:1882` | `tests/test_portability_v1.py::test_cmd_analytics_exists` | TG already had `/analytics` |
| 2.24.0 | `--pipeline N` | N/A | - | - | single-request bet API; concurrent bets break balance tracking and the reply carries no balance |
| 2.25.0 | max-nonce recovery | N/A | - | - | WolfBet seed / nonce API; the service has no max-nonce error |
| 2.25.0 | `--config-show`, `--config-set`, `--version` | Ported | `stake.py:6255`, `stake.py:6319`, `stake.py:6803` | `tests/test_wolfbet_sync_e10.py::test_config_set_scope_stored_and_validated` |  |
| 2.25.0 | monitor ROI / p99 | Ported | `stake.py:1541`, `stake.py:5224` | `tests/test_wolfbet_sync_e3.py::test_pause_label_and_monitor_source` |  |
| 2.25.1 - 2.25.2 | ctl subcommands, `--autostash` | Ported | `stakectl:1139`, `stakectl:803` | `tests/test_wolfbet_sync_e6.py::test_ctl_syntax` |  |
| 2.25.3 | `recurring_delay` | Ported | `stake.py:709` | `tests/test_wolfbet_sync_e2.py::test_recurring_keeps_recurring_delay` | TG already had it (no cap) |
| 2.26.0 | proxy, `purge_days` + `--cleanup` | Ported | `stake.py:23`, `stake.py:6504`, `stake.py:6525` | `tests/test_wolfbet_sync_e5.py::test_cleanup_purges_old_sessions_only` | saved proxy now applied on `--resume` |
| 2.26.1 | `--benchmark`, `--diagnose` | Adapted | `stake.py:6588`, `stake.py:6675` | `tests/test_portability_v1.py::test_cmd_diagnose_exists` | probe = the service's read-only endpoint |
| 2.27.0 | async bet writer + PRAGMAs | Ported | `stake.py:2857`, `stake.py:2931` | `tests/test_wolfbet_sync_e5.py::test_main_startup_purge_and_threads` | PRAGMAs were already there; TG already batched |
| 2.28.0 | helpers moved to `core/` | Adapted | `stake.py:15` | `tests/test_portability_v1.py::test_tg_analytics_threads_where` | pure helpers live in casino-shared (`shared.core.*`) |
| 2.28.1 | monitor footer hints, systemd-aware stop | Ported | `stake.py:5405`, `stake.py:5798` | `tests/test_wolfbet_sync_e5.py::test_is_systemd_managed_reads_cgroup` |  |
| 2.28.2 | config persistence gap | Ported | `stake.py:784` | `tests/test_wolfbet_sync_e10.py::test_run_profit_sums_lane_sessions_and_bumps_persist` |  |
| 2.28.3 | SIGHUP live reload | Ported | `stake.py:860`, `stake.py:909` | `tests/test_wolfbet_sync_e10.py::test_live_reload_increment_off_ends_run` |  |
| 2.28.4 | uptime freezes in recurring pause | Ported | `stake.py:1493`, `tg/engine.py:208` | `tests/test_wolfbet_sync_e3.py::test_uptime_freezes_during_pause` | TG already had it |
| 2.29.0 | profit increment restarts the auto cycle | N/A | - | - | no server auto-bet cycle; the bump already applies to the next bet |
| 2.30.0 | CFG row | Ported | `stake.py:3164` | `tests/test_wolfbet_sync_e10.py::test_progress_row_uses_run_total` | pre-existing |
| 2.30.1 | persist `next_profit_milestone`, reset per cycle | Ported | `stake.py:774`, `tg/engine.py:278` | `tests/test_wolfbet_sync_e10.py::test_session_scope_keeps_milestone_reset_in_recurring` | the CFG `Ms` segment now reads the persisted milestone |
| 2.30.2 | ctl update self-heal | Ported | `stakectl:1139` | `tests/test_wolfbet_sync_e7.py::test_update_autostashes_reinstalls_and_restarts_what_ran` |  |
| 2.30.3 - 2.30.5 | metric correctness, windowed cycle / efficiency | Ported | `stake.py:1560`, `stake.py:1593` | `tests/test_wolfbet_sync_e5.py::test_metrics_recorded_per_bet` |  |
| wolfctl 3.7 - 3.8 | `doctor [--fix]`, `Nice=-5` | Ported | `stakectl:1192`, `install.sh:111` | `tests/test_wolfbet_sync_e7.py::test_doctor_reports_flaresolverr_as_info_only` |  |
| 2.30.6 | `recurring_delay` cap 86400 + visible clamps | Ported | `stake.py:16`, `tg/handlers.py:42` | `tests/test_wolfbet_sync_e2.py::test_recurring_delay_capped_at_stop` | CLI and TG |
| 2.30.7 | `Ms` per-window progress | Ported | `stake.py:3183` | `tests/test_wolfbet_sync_e3.py::test_cfg_ms_per_window_progress` |  |
| 2.30.8 | `bet_value` / 422 | N/A | - | - | WolfBet bet_value validation; the service has no such field |
| 2.30.9 | ctl `reset` / `archives`, `--stats --since / --until` | Ported | `stakectl:1193`, `stakectl:1195`, `stake.py:6839` | `tests/test_wolfbet_sync_e7.py::test_reset_archives_and_never_deletes` |  |
| security 2026-11-15 | default web credentials refused, per-user web authz, WS `_connect_ro`, owner gate, `nan` / `inf` limits, pinned `ctl update`, admin secret rendering, API keys out of logs | Adapted | `web/config.py:12`, `web/bot_db.py:9`, `stake.py:778` | `tests/test_security_hotfix.py::test_other_user_is_forbidden` | same intent, service code paths |
| security 2026-11-15 | service-specific guards | Ported | `stake.py:96` | `tests/test_security_hotfix.py::test_worker_requires_the_shared_secret` | FlareSolverr loopback only, Worker key, one browser |
| 2.31.0 | date-range analytics | Ported | `stake.py:5836` | `tests/test_wolfbet_sync_e3.py::test_cfg_ms_per_window_progress` | TG already had it |
| 2.32.0 | streak reset | Ported | `stake.py:18`, `tg/config.py:56` | `tests/test_wolfbet_sync_e2.py::test_streak_reset_fires_on_breaking_win_at_profit` | CLI and TG |
| 2.32.1 | insufficient balance is terminal on auto | Adapted | `stake.py:1423`, `tg/engine.py:263` | `tests/test_wolfbet_sync_e0.py::test_cli_insufficient_balance_stops` | terminal stop + chain-depth warning; mid-chain auto guard N/A (no server auto-bet) |
| 2.33.0 | time-of-day window fix | Ported | `stake.py:20` | `tests/test_wolfbet_sync_e4.py::test_since_with_time_of_day_excludes_earlier_session` | CLI and TG |
| 2.33.1 | wins / losses per session | Ported | `stake.py:649` | `tests/test_wolfbet_sync_e4.py::test_wins_losses_per_session_window_aware` |  |
| 2.34.0 | durations, time span, run time | Ported | `stake.py:20` | `tests/test_wolfbet_sync_e3.py::test_run_uptime_spans_cycles` |  |
| 2.35.0 | parallel lanes: `--lane K`, laneK paths, `[LK]` prefix | Ported | `stake.py:524` | `tests/test_wolfbet_sync_e5_lanes.py::test_apply_lane_switches_paths_and_log_prefix` |  |
| 2.35.0 | `sessions.lane` column | Ported | `stake.py:694` | `tests/test_wolfbet_sync_e4.py::test_init_db_adds_lane_to_old_db_idempotent` | schema lives in casino-shared `SESSIONS_SCHEMA` |
| 2.35.0 | split budget, `Lane budget exceeded` loss stop | Ported | `stake.py:2467`, `stake.py:29` | `tests/test_wolfbet_sync_e5_lanes.py::test_init_lane_budget` |  |
| 2.35.0 | per-lane overrides, `config-set lanes=N` / `laneK.key` | Ported | `stake.py:6450` | `tests/test_wolfbet_sync_e5_lanes.py::test_load_config_applies_lane_overrides` |  |
| 2.35.0 | lane start stagger | Ported | `stake.py:7008` | `tests/test_wolfbet_sync_e5_lanes.py::test_lane_start_stagger_and_budget` |  |
| 2.35.0 | soft pacing from `x-ratelimit-*` headers | N/A | - | - | the service returns no quota headers; each lane keeps the 429 backoff |
| 2.35.0 | `/auto/start` cross-lane lock | N/A | - | - | no server auto-bet |
| 2.35.0 | `pipeline_depth` forbidden key | N/A | - | - | no pipeline |
| 2.35.0 | `--benchmark --lanes M` | Adapted | `stake.py:6608` | `tests/test_wolfbet_sync_e5_lanes.py::test_benchmark_lanes_probes_user_balances` | read-only probe at 1..M concurrent clients |
| 2.35.0 | `--stats` / `--analytics --lane K`, analytics By lane | Ported | `stake.py:6047` | `tests/test_wolfbet_sync_e4.py::test_stats_lane_filter_and_tag` |  |
| wolfctl 3.10 | ctl `lanes [N]`, all-lane start / stop / restart / update / reset | Ported | `stakectl:1174` | `tests/test_wolfbet_sync_e7.py::test_lanes_n_writes_config_and_enables_lane_units` |  |
| 2.35.0 | service-specific lanes | Adapted | `stake.py:97`, `stake.py:155` | `tests/test_wolfbet_sync_e5_lanes.py::test_two_lanes_hit_cf_403_together_one_reauth` | CF cookie file written atomically; `_cf_reauth` serialised across lanes by a lock file |
| 2.36.0 | `lane_split` none / budget / bet / both | Ported | `stake.py:811`, `stake.py:28` | `tests/test_wolfbet_sync_e10.py::test_split_base_bet_gets_the_bump` | a divided bet below the service minimum stays full |
| wolfctl 3.11 | ctl `lanes N --split MODE` | Ported | `stakectl:203` | `tests/test_wolfbet_sync_e10.py::test_split_base_bet_gets_the_bump` |  |
| 2.37.0 | `profit_increment_scope` / `piscope` session / run, run file, DB sum, resume bumps | Ported | `stake.py:2342`, `stake.py:2369`, `stake.py:485` | `tests/test_wolfbet_sync_e10.py::test_stale_run_file_after_db_reset_starts_new_run` | CLI; TG adapted by request |
| 2.37.0 | status `Run Profit`, monitor `RunMs` | Ported | `stake.py:5765`, `stake.py:3183` | `tests/test_wolfbet_sync_e10.py::test_progress_row_uses_run_total` |  |
| 2.37.0 | `--run-reset`, ctl `run-reset`, `reset` clears run files | Ported | `stake.py:2454`, `stakectl:1194`, `tg/handlers.py:1109` | `tests/test_wolfbet_sync_e10.py::test_run_reset_removes_files` |  |
| 2.37.0 | auto-UUID restart on bump | N/A | - | - | no server auto-bet; the bumped base applies to the next bet |
| 2.37.1 | UUID swap waits for non-negative profit and streak | N/A | - | `tests/test_wolfbet_sync_e3.py::test_streak_reset_in_red_does_not_stop` | no server auto-bet, UUID or mid-chain guard; proven by the regression test instead |
| 2.37.1 | `Stop reason` row + `DAEMON EXIT ... reason=` | Ported | `stake.py:4693`, `stake.py:7104` | `tests/test_wolfbet_sync_e6.py::test_summary_shows_the_stop_reason` |  |
| 2.37.1 | `install.sh` enables the CLI unit (boot only) | Ported | `install.sh:223` | `tests/test_wolfbet_sync_e7.py::test_lanes_n_writes_config_and_enables_lane_units` | lane units too; `doctor` flags a disabled unit |
| 2.37.2 | bumps belong to the base they were earned on | Ported | `stake.py:2439`, `stake.py:827` | `tests/test_wolfbet_sync_e10.py::test_live_base_change_zeroes_bumps_and_reanchors` | live base reload judged against the configured base; run file keeps `base` |
| TG v1.21.0 | TG run-wide increment, parity with CLI 2.37.0 | Adapted | `tg/config.py:54`, `tg/handlers.py:1109` | `tests/test_wolfbet_sync_e10.py::test_tg_run_created_at_first_session` | WolfBet TG lacked it; added by request following v1.21.0 |

## Known gaps

Found by the cross-service regression suite (`tests/test_wolfbet_sync_e8_regression.py`); recorded, not fixed here.

- CLI user pause (SIGUSR1 / `p`) does not freeze session uptime; the regression test is a strict xfail.
- CLI has no mid-session `cf_403` re-auth (TG has it); the regression test is a strict xfail.
