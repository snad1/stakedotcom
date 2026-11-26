"""stakectl + install.sh operations (stakectl v2.2, lanes from v1.2), mirroring wolfctl 3.12.

The ctl runs for real under bash against a temp HOME. systemctl is a stub on
PATH that keeps unit state in files (active / enabled), and the bot's python
is a stub that logs its arguments (heredoc calls run the real interpreter so
lane_count and lane_split_mode still read the config). No real systemd, no
real DB, no network.

Run: APP_ENV=testing .venv-test/bin/python -m pytest stake/tests -q
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CTL = _ROOT / "stakectl"
_INSTALL = _ROOT / "install.sh"

_SYSTEMCTL = r'''#!/usr/bin/env bash
echo "$*" >> "$HOME/systemctl.log"
[ "${1:-}" = "--user" ] && shift
verb="${1:-}"; shift || true
now=0; quiet=0; units=()
for a in "$@"; do
    case "$a" in --now) now=1 ;; --quiet|--no-pager) quiet=1 ;; -*) ;; *) units+=("$a") ;; esac
done
st="$HOME/units"; mkdir -p "$st"
for u in ${units[@]+"${units[@]}"}; do
    u="${u%.service}"
    case "$verb" in
        start|restart) touch "$st/$u.active" ;;
        stop) rm -f "$st/$u.active" ;;
        enable) touch "$st/$u.enabled"; [ "$now" = 1 ] && touch "$st/$u.active" ;;
        disable) rm -f "$st/$u.enabled"; [ "$now" = 1 ] && rm -f "$st/$u.active" ;;
        is-active)
            if [ -f "$st/$u.active" ]; then [ "$quiet" = 1 ] || echo active
            else [ "$quiet" = 1 ] || echo inactive; exit 3; fi ;;
        is-enabled) [ -f "$st/$u.enabled" ] || exit 1 ;;
        status) [ -f "$st/$u.active" ] && echo "active" || exit 3 ;;
    esac
done
exit 0
'''

_PYTHON = r'''#!/usr/bin/env bash
if [ "${1:-}" = "-" ] || [ "${1:-}" = "-c" ]; then
    exec "__REAL__" "$@"
fi
echo "$*" >> "$HOME/py.log"
case "$*" in
    *--config-set*|*--lane-add*|*--lane-remove*)
        # A test that wants the bot's write to land puts the resulting config in config-after.json.
        [ -f "$HOME/config-after.json" ] && mv "$HOME/config-after.json" "$HOME/.stake_autobot.json"
        exit 0 ;;
    *--key-put*|*--key-rotate*)
        cat > "$HOME/stdin.txt"
        exit 0 ;;
    *--lane-ids*)
        # The registry is read the way stake.py reads it, from the real config file.
        exec "__REAL__" -c 'import json, os, sys
from shared.core.lanes import lane_registry
try:
    cfg = json.load(open(os.path.expanduser("~/.stake_autobot.json")))
except Exception:
    cfg = {}
reg = lane_registry(cfg)
print(" ".join(str(k) for k in sorted(reg) if sys.argv[1] == "all" or reg[k]["enabled"]))' "${3:-enabled}" ;;
esac
exit 0
'''


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    bindir = tmp_path / "bin"
    venv = home / "stake-bot" / "venv" / "bin"
    for d in (home, bindir, venv):
        d.mkdir(parents=True)
    (bindir / "systemctl").write_text(_SYSTEMCTL)
    (bindir / "journalctl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bindir / "docker").write_text('#!/usr/bin/env bash\necho "docker $*" >> "$HOME/docker.log"\nexit 0\n')
    (bindir / "curl").write_text("#!/usr/bin/env bash\nexit 7\n")
    (venv / "python3").write_text(_PYTHON.replace("__REAL__", sys.executable))
    for f in (bindir / "systemctl", bindir / "journalctl", bindir / "pgrep", bindir / "docker",
              bindir / "curl", venv / "python3"):
        f.chmod(0o755)
    e = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    e.pop("STAKE_RUN_PATH", None)
    e.pop("FLARESOLVERR_URL", None)
    return {"home": home, "env": e, "bin": bindir}


def _ctl(env, *args, stdin="", cwd=None, ctl=_CTL):
    return subprocess.run(["bash", str(ctl), *args], input=stdin, capture_output=True,
                          text=True, env=env["env"], cwd=cwd or env["home"], timeout=30)


def _log(env, name):
    p = env["home"] / name
    return p.read_text() if p.exists() else ""


def _config(env, lanes=1, mode=0o600):
    p = env["home"] / ".stake_autobot.json"
    p.write_text(json.dumps({"base_bet": 0.001, "lanes": {"count": lanes, "split": "budget"}}))
    p.chmod(mode)
    return p


def _units(env):
    d = env["home"] / "units"
    return {p.name for p in d.iterdir()} if d.exists() else set()


# ── syntax + usage ───────────────────────────────────────
def test_scripts_parse():
    for f in (_CTL, _INSTALL):
        assert subprocess.run(["bash", "-n", str(f)]).returncode == 0


def test_version_constant_is_grep_parsable():
    assert "\nSTAKECTL_VERSION=2.7\n" in _CTL.read_text()


@pytest.mark.parametrize("cmd", [
    "lanes", "reset", "run-reset", "archives", "doctor", "refresh", "version",
    "config-show", "config-set", "cleanup", "benchmark", "diagnose", "last-bets", "preset",
])
def test_usage_lists_each_new_subcommand(env, cmd):
    assert f"stakectl {cmd}" in _ctl(env).stdout


# ── pass-throughs ────────────────────────────────────────
@pytest.mark.parametrize("args,flags", [
    (["config-show"], "--config-show"),
    (["config-set", "maxloss=5"], "--config-set maxloss=5"),
    (["cleanup"], "--cleanup"),
    (["cleanup", "7"], "--cleanup 7"),
    (["benchmark", "20", "--lanes", "2"], "--benchmark 20 --lanes 2"),
    (["diagnose"], "--diagnose"),
    (["last-bets"], "--last-bets 20"),
    (["last-bets", "5"], "--last-bets 5"),
    (["preset", "safe"], "--preset safe --daemon"),
    (["monitor", "2"], "--monitor --lane 2"),
    (["stats", "--lane", "2"], "--stats --lane 2"),
    (["analytics", "--lane", "3"], "--analytics --lane 3"),
    (["version"], "--version"),
])
def test_pass_through_calls_the_bot_flag(env, args, flags):
    res = _ctl(env, *args)
    assert res.returncode == 0, res.stderr
    assert flags in _log(env, "py.log")


def test_version_prints_the_ctl_version(env):
    assert "stakectl v2.7" in _ctl(env, "version").stdout


def test_config_set_without_pairs_shows_usage(env):
    res = _ctl(env, "config-set")
    assert res.returncode == 2 and "Usage: stakectl config-set" in res.stdout


# ── reset / archives / run-reset ─────────────────────────
def _stats_files(env):
    h = env["home"]
    (h / ".stake_autobot.db").write_text("db")
    for name in (".stake_autobot_live.json", ".stake_autobot_live.lane2.json",
                 ".stake_run.json", ".stake_run.lane2.json"):
        (h / name).write_text("{}")


def test_reset_archives_and_never_deletes(env):
    _config(env)
    _stats_files(env)
    res = _ctl(env, "reset", "--yes")
    h = env["home"]
    archives = list(h.glob(".stake_autobot-*.db"))
    assert res.returncode == 0, res.stderr
    assert len(archives) == 1 and archives[0].read_text() == "db"
    assert not (h / ".stake_autobot.db").exists()
    assert not list(h.glob(".stake_autobot_live*.json"))
    assert not list(h.glob(".stake_run*.json"))
    assert (h / ".stake_autobot.json").exists()
    assert "Archived ->" in res.stdout


def test_reset_answer_no_changes_nothing(env):
    _stats_files(env)
    res = _ctl(env, "reset", stdin="n\n")
    assert "Aborted." in res.stdout
    assert (env["home"] / ".stake_autobot.db").exists()
    assert not list(env["home"].glob(".stake_autobot-*.db"))


def test_reset_stops_a_running_daemon_and_its_lanes(env):
    _config(env, lanes=2)
    _stats_files(env)
    _ctl(env, "reset", "--yes")  # nothing running: no stop
    assert "stop stake\n" not in _log(env, "systemctl.log")
    _stats_files(env)
    (env["home"] / "units").mkdir(exist_ok=True)
    (env["home"] / "units" / "stake.active").touch()
    res = _ctl(env, "reset", "--yes")
    log = _log(env, "systemctl.log")
    assert "Daemon stopped." in res.stdout
    assert "--user stop stake-lane@2" in log and "--user stop stake\n" in log


def test_archives_lists_and_empty_case(env):
    assert "No archives found" in _ctl(env, "archives").stdout
    (env["home"] / ".stake_autobot-20261123-101010.db").write_text("x")
    assert ".stake_autobot-20261123-101010.db" in _ctl(env, "archives").stdout


def test_run_reset_calls_the_bot_and_restarts_lanes(env):
    _config(env, lanes=2)
    (env["home"] / "units").mkdir()
    (env["home"] / "units" / "stake.active").touch()
    res = _ctl(env, "run-reset", "--yes")
    assert "--run-reset" in _log(env, "py.log")
    assert "stake.active" in _units(env) and "stake-lane@2.active" in _units(env)
    assert "Lanes restarted on a new run." in res.stdout


def test_run_reset_answer_no_does_nothing(env):
    res = _ctl(env, "run-reset", stdin="n\n")
    assert "Aborted." in res.stdout and "--run-reset" not in _log(env, "py.log")


# ── lanes ────────────────────────────────────────────────
def _after(env, lanes):
    (env["home"] / "config-after.json").write_text(
        json.dumps({"base_bet": 0.001, "lanes": {"count": lanes, "split": "budget"}}))


def test_lanes_n_on_a_stopped_bot_starts_nothing_and_says_how(env):
    _config(env, lanes=1)
    _after(env, 3)
    res = _ctl(env, "lanes", "3", "--split", "budget")
    assert "--config-set lanes=3 lane_split=budget" in _log(env, "py.log")
    assert not {u for u in _units(env) if u.startswith("stake-lane@")}
    assert "lane 3 ready; start it with: stakectl start 3" in res.stdout


def test_lanes_n_on_a_running_bot_starts_only_the_new_lanes(env):
    _config(env, lanes=1)
    _after(env, 3)
    (env["home"] / "units").mkdir()
    (env["home"] / "units" / "stake.active").touch()
    res = _ctl(env, "lanes", "3")
    assert {"stake-lane@2.active", "stake-lane@3.active"} <= _units(env)
    unit = (env["home"] / ".config/systemd/user/stake-lane@.service").read_text()
    assert "--lane %i" in unit and "Nice=-5" in unit and "WantedBy=default.target" in unit
    log = _log(env, "systemctl.log")
    assert "restart stake" not in log and "stop stake\n" not in log
    assert "lane 2 started" in res.stdout and "Budget shares update when each lane restarts" in res.stdout


def test_lanes_1_stops_only_lanes_two_and_up(env):
    _config(env, lanes=3)
    _after(env, 1)
    (env["home"] / "units").mkdir()
    for u in ("stake", "stake-lane@2", "stake-lane@3"):
        (env["home"] / "units" / f"{u}.enabled").touch()
        (env["home"] / "units" / f"{u}.active").touch()
    _ctl(env, "lanes", "1")
    assert not {u for u in _units(env) if u.startswith("stake-lane@")}
    assert "stake.active" in _units(env)


def test_lanes_rejects_bad_input(env):
    assert _ctl(env, "lanes", "x").returncode == 1
    assert _ctl(env, "lanes", "--max", "x").returncode == 1
    assert _ctl(env, "lanes", "2", "--split", "half").returncode == 1


def test_start_and_stop_act_on_every_lane(env):
    _config(env, lanes=3)
    res = _ctl(env, "start")
    assert "Bot started with 3 lanes." in res.stdout
    assert {"stake.active", "stake-lane@2.active", "stake-lane@3.active"} <= _units(env)
    _ctl(env, "stop")
    assert not {u for u in _units(env) if u.endswith(".active")}


def test_start_on_an_active_unit_says_nothing_restarted(env):
    _config(env)
    _ctl(env, "start")
    assert "already running - nothing restarted" in _ctl(env, "start").stdout


def test_status_adds_the_lane_table_with_lanes(env):
    _config(env, lanes=1)
    _ctl(env, "status")
    assert "--lanes-status" not in _log(env, "py.log")
    _config(env, lanes=2)
    _ctl(env, "status")
    assert "--lanes-status" in _log(env, "py.log")


def test_start_all_and_stop_all_cover_the_lanes(env):
    _config(env, lanes=2)
    _ctl(env, "start-all")
    assert {"stake.active", "stake-lane@2.active"} <= _units(env)
    _ctl(env, "stop-all")
    assert "stake-lane@2.active" not in _units(env) and "stake.active" not in _units(env)


# ── Cloudflare tooling stays ─────────────────────────────
def test_flaresolverr_still_dispatches(env):
    res = _ctl(env, "flaresolverr")
    assert res.returncode == 0 and "FlareSolverr: NOT RUNNING" in res.stdout
    _ctl(env, "flaresolverr", "stop")
    assert "docker stop flaresolverr" in _log(env, "docker.log")
    assert "stakectl flaresolverr start" in _ctl(env).stdout


def test_doctor_reports_flaresolverr_as_info_only(env):
    _config(env)
    out = _ctl(env, "doctor", "--fix", "--yes").stdout
    assert "[INFO]  FlareSolverr not reachable at http://localhost:8191/v1" in out
    assert "[INFO]  flaresolverr container not running" in out
    fixes = out[out.index("Applying safe fixes"):]
    assert "flaresolverr" not in fixes.lower()
    assert "docker" not in _log(env, "docker.log").replace("docker ps", "")


def test_doctor_uses_flaresolverr_url_from_the_env(env):
    _config(env)
    env["env"]["FLARESOLVERR_URL"] = "http://10.0.0.5:8191/v1"
    assert "not reachable at http://10.0.0.5:8191/v1" in _ctl(env, "doctor").stdout


def test_installer_has_no_inline_ctl_and_keeps_flaresolverr():
    src = _INSTALL.read_text()
    assert "CTLEOF" not in src
    assert "-p 127.0.0.1:8191:8191" in src and "flaresolverr:v3.5.2" in src


def test_warp_and_wireguard_scripts_unchanged():
    for name in ("warp-setup.sh", "wireguard-setup.sh"):
        res = subprocess.run(["git", "-C", str(_ROOT), "diff", "--quiet", "HEAD", "--", name])
        assert res.returncode == 0, f"{name} changed"


# ── doctor ───────────────────────────────────────────────
def test_doctor_warns_on_a_disabled_unit_and_loose_config(env):
    _config(env, mode=0o644)
    out = _ctl(env, "doctor").stdout
    assert "[WARN]  stake.service is not enabled" in out
    assert "systemctl --user enable stake" in out
    assert "[WARN]  config mode 644" in out
    assert "stakectl doctor --fix" in out


def test_doctor_ok_when_enabled_and_private(env):
    _config(env)
    (env["home"] / "units").mkdir()
    (env["home"] / "units" / "stake.enabled").touch()
    out = _ctl(env, "doctor").stdout
    assert "[OK]    stake.service enabled" in out and "[OK]    config mode 600" in out


def test_doctor_fix_applies_safe_fixes(env):
    cfg = _config(env, mode=0o644)
    res = _ctl(env, "doctor", "--fix", "--yes")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert "stake.enabled" in _units(env)
    assert "--cleanup 1" in _log(env, "py.log")
    assert "Applying safe fixes" in res.stdout


def test_doctor_has_the_virtualized_skip_and_no_privileged_auto_fix():
    src = _CTL.read_text()
    body = src[src.index("cmd_doctor() {"):src.index("_install_ctl() {")]
    assert "[SKIP]" in body and "systemd-detect-virt" in body
    fixes = body[body.index('if [ "$do_fix" = "1" ]'):]
    assert "renice" not in fixes and "cpupower" not in fixes and "sudo" not in fixes


# ── update / refresh ─────────────────────────────────────
def test_update_autostashes_reinstalls_and_restarts_what_ran():
    src = _CTL.read_text()
    body = src[src.index("cmd_update() {"):src.index("cmd_tg() {")]
    assert "git pull --ff-only --autostash" in body
    assert 'install --quiet --upgrade -r "$INSTALL_DIR/requirements.txt"' in body
    assert "stopping before the restart" in body
    assert "Restart=always -> Restart=on-failure" in body and "Nice=-5" in body
    assert "sync_lanes" in body and "_install_ctl" in body
    assert "stakectl was stale" in body


def test_refresh_installs_a_newer_repo_copy(env, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    newer = _CTL.read_text().replace("\nSTAKECTL_VERSION=2.7\n", "\nSTAKECTL_VERSION=9.9\n")
    (repo / "stakectl").write_text(newer)
    local_bin = env["home"] / ".local" / "bin"
    local_bin.mkdir(parents=True)
    installed = local_bin / "stakectl"
    installed.write_text(_CTL.read_text())
    env["env"]["PATH"] = f"{local_bin}:{env['env']['PATH']}"

    res = _ctl(env, "refresh", cwd=repo)

    assert "stakectl 2.7 -> 9.9 installed at" in res.stdout
    assert installed.read_text() == newer
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert "already current" in _ctl(env, "refresh", cwd=repo).stdout


def test_refresh_outside_the_repo_says_so(env):
    res = _ctl(env, "refresh")
    assert res.returncode == 1 and "run this from the stake repo" in res.stdout


# ── install.sh ───────────────────────────────────────────
def test_install_has_no_dead_shared_copy():
    assert "../shared" not in _INSTALL.read_text()


def test_install_writes_the_lane_template_and_nice():
    src = _INSTALL.read_text()
    assert '${LANE_SERVICE_NAME}@.service' in src and "--lane %i" in src
    assert src.count("Nice=-5") == 2


def test_install_enables_the_cli_unit_for_boot_only():
    src = _INSTALL.read_text()
    assert "systemctl --user enable ${SERVICE_NAME} " in src
    assert "enable --now ${SERVICE_NAME}" not in src
    assert src.index("daemon-reload") < src.index("enable ${SERVICE_NAME}")


def test_install_installs_the_ctl_with_mode_0755():
    src = _INSTALL.read_text()
    assert 'install -m 0755 stakectl "$STAKECTL"' in src
    assert 'cp stakectl "$STAKECTL"' not in src
