"""Security hotfixes: web credentials, per-user authz, owner gate, finite limits.

Run: APP_ENV=testing .venv-test/bin/python -m pytest stakedotcom/tests/test_security_hotfix.py -q
"""

import asyncio
import os
import sqlite3
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import stake as cli  # noqa: E402
from tg import handlers as tg_handlers  # noqa: E402
from web import auth as web_auth  # noqa: E402
from web import bot_db as web_bot_db  # noqa: E402
from web import config as web_config  # noqa: E402
from web import database as web_db  # noqa: E402
from web.routes import api as web_api  # noqa: E402

OWNER_ID = 4242
USER_ID = 111
OTHER = 222
SESSION_ID = 1

USER_ROUTES = [
    f"/api/users/{USER_ID}/sessions",
    f"/api/users/{USER_ID}/sessions/csv",
    f"/api/users/{USER_ID}/sessions/{SESSION_ID}",
    f"/api/users/{USER_ID}/sessions/{SESSION_ID}/bets",
    f"/api/users/{USER_ID}/sessions/{SESSION_ID}/bets/csv",
    f"/api/users/{USER_ID}/sessions/{SESSION_ID}/chart",
]


class TestWebCredentials:
    @pytest.mark.parametrize("secret_key,admin_pass", [
        ("change-me-to-a-random-64-char-string", "a-strong-password"),
        ("x" * 31, "a-strong-password"),
        ("s" * 40, "changeme123"),
        ("s" * 40, "short-pass1"),
    ])
    def test_weak_pair_refused(self, secret_key, admin_pass):
        with pytest.raises(ValueError):
            web_config.Settings(_env_file=None, secret_key=secret_key, admin_pass=admin_pass)

    def test_strong_pair_accepted(self):
        settings = web_config.Settings(
            _env_file=None, secret_key="s" * 40, admin_pass="a-strong-password")
        assert settings.admin_pass == "a-strong-password"


def _bot_db(data_dir, user_id):
    user_dir = os.path.join(data_dir, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(user_dir, DB_FILENAME))
    conn.executescript(
        """
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, total_bets INTEGER, profit REAL,
                               wins INTEGER, losses INTEGER, strategy TEXT,
                               wagered REAL, started_at TEXT, ended_at TEXT);
        CREATE TABLE bets (
            id INTEGER PRIMARY KEY, session_id INTEGER, timestamp TEXT, amount REAL,
            multiplier REAL, multiplier_target REAL, bet_value REAL, rule TEXT,
            result_value REAL, result_display TEXT, state TEXT, profit REAL,
            balance_after REAL
        );
        """
    )
    conn.execute("INSERT INTO sessions (id, total_bets, profit) VALUES (1, 0, 0.0)")
    conn.execute(
        "INSERT INTO bets (id, session_id, timestamp, amount, multiplier,"
        " multiplier_target, state) VALUES (1, 1, '2026-11-15T10:00:00', 1.0, 2.0, 2.0, 'win')"
    )
    conn.commit()
    conn.close()


DB_FILENAME = "stake.db"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_config.settings, "db_path", str(tmp_path / "web.db"))
    data_dir = tmp_path / "botdata"
    _bot_db(data_dir, USER_ID)
    _bot_db(data_dir, OTHER)
    monkeypatch.setattr(web_config.settings, "bot_data_dir", str(data_dir))
    web_db.init_db()

    app = FastAPI()
    app.include_router(web_api.router)
    return TestClient(app)


def _token(subject, role):
    jti = web_db.create_web_session("test", subject, "2099-01-01T00:00:00+00:00")
    return web_auth.create_token(subject=subject, role=role, jti=jti)


@pytest.mark.parametrize("path", USER_ROUTES)
def test_owner_reads_own_data(client, path):
    client.cookies.set(web_auth.COOKIE_NAME, _token(f"tg:{USER_ID}", "tg_user"))
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", [p.replace(str(USER_ID), str(OTHER)) for p in USER_ROUTES])
def test_other_user_is_forbidden(client, path):
    client.cookies.set(web_auth.COOKIE_NAME, _token(f"tg:{USER_ID}", "tg_user"))
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", USER_ROUTES)
def test_admin_reads_any_user(client, path):
    client.cookies.set(web_auth.COOKIE_NAME, _token("admin:1", "admin"))
    assert client.get(path).status_code == 200


def test_token_without_jti_rejected(client):
    token = jwt.encode(
        {"sub": f"tg:{USER_ID}", "role": "tg_user", "exp": 4102444800},
        web_config.settings.secret_key, algorithm="HS256")
    client.cookies.set(web_auth.COOKIE_NAME, token)
    assert client.get(f"/api/users/{USER_ID}/sessions").status_code == 401


def test_bot_db_exports_a_bound_connect_ro(tmp_path, monkeypatch):
    data_dir = tmp_path / "botdata"
    _bot_db(data_dir, USER_ID)
    monkeypatch.setattr(web_config.settings, "bot_data_dir", str(data_dir))
    conn = web_bot_db._connect_ro(USER_ID)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    conn.close()


class TestOwnerGate:
    def test_unset_env_owns_nobody(self, monkeypatch):
        monkeypatch.delenv("TG_OWNER_ID", raising=False)
        assert tg_handlers._is_owner(680974641) is False

    def test_only_the_configured_id(self, monkeypatch):
        monkeypatch.setenv("TG_OWNER_ID", str(OWNER_ID))
        assert tg_handlers._is_owner(OWNER_ID) is True
        assert tg_handlers._is_owner(OWNER_ID + 1) is False

    def test_update_refused_without_env(self, monkeypatch):
        monkeypatch.delenv("TG_OWNER_ID", raising=False)
        launched = []
        monkeypatch.setattr(tg_handlers.subprocess, "run",
                            lambda cmd, **kw: launched.append(cmd) or types.SimpleNamespace(
                                returncode=0, stdout="", stderr=""))
        sent = []

        async def reply_text(text, **kw):
            sent.append(text)

        upd = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=680974641),
            message=types.SimpleNamespace(reply_text=reply_text))
        asyncio.run(tg_handlers.cmd_update(upd, types.SimpleNamespace(args=[])))
        assert sent == ["Owner only."]
        assert launched == []

    def test_argv_has_no_shell(self):
        argv = tg_handlers._update_argv("/usr/local/bin/stakectl", "/root/stakedotcom", "/d/update.log")
        assert argv[0] == "systemd-run"
        assert "bash" not in argv and "-c" not in argv
        assert argv[-2:] == ["/usr/local/bin/stakectl", "update"]

    def test_hostile_path_stays_one_argument(self):
        hostile = "/root/stakedotcom; touch /tmp/pwned"
        argv = tg_handlers._update_argv("/usr/local/bin/stakectl", hostile, "/d/update.log")
        assert f"--working-directory={hostile}" in argv
        assert "touch" not in argv


class TestFiniteLimits:
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_config_set_rejects_non_finite(self, value):
        with pytest.raises(ValueError):
            cli._apply_mutation({"max_loss": value})

    @pytest.mark.parametrize("value", ["nan", "inf"])
    def test_set_parser_rejects_non_finite(self, value):
        with pytest.raises(ValueError):
            tg_handlers._finite(value)

    def test_set_parser_accepts_numbers(self):
        assert tg_handlers._finite("2.5") == 2.5
        assert tg_handlers._finite("7", int) == 7


class TestSecretRendering:
    def test_admin_route_declares_the_secret_keys(self):
        from web.routes import admin_routes
        assert {"api_key", "token", "proxy"} <= admin_routes.CONFIG_SECRET_KEYS

    def test_template_masks_them(self):
        template = open(os.path.join(_ROOT, "web/templates/admin/user_detail.html"), encoding="utf-8").read()
        assert "key in secret_keys" in template
        assert "hidden" in template


class TestCtlUpdate:
    def test_pinned_requirements_and_loud_failure(self):
        ctl = open(os.path.join(_ROOT, "stakectl"), encoding="utf-8").read()
        assert "--force-reinstall" not in ctl
        assert "stopping before the restart" in ctl


class TestCloudflareSurfaces:
    def test_chrome_args_drop_the_sandbox_only_for_root(self, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        assert "--no-sandbox" not in cli._chrome_args()
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert "--no-sandbox" in cli._chrome_args()

    def test_lock_times_out(self, monkeypatch):
        import fcntl
        monkeypatch.setattr(cli, "CF_LOCK_TIMEOUT", 0.0)

        def _blocked(*a, **kw):
            raise BlockingIOError

        monkeypatch.setattr(fcntl, "flock", _blocked)
        with pytest.raises(ConnectionError, match="another process"):
            with cli._cf_reauth_lock():
                pass

    def test_worker_requires_the_shared_secret(self):
        worker = open(os.path.join(_ROOT, "cloudflare-worker/proxy.js"), encoding="utf-8").read()
        assert "X-Proxy-Key" in worker and "env.PROXY_KEY" in worker

    def test_stake_sends_the_proxy_key(self, monkeypatch):
        monkeypatch.setenv("STAKE_PROXY_KEY", "abc")
        assert cli._headers().get("X-Proxy-Key") == "abc"

    def test_installer_publishes_loopback_only_and_pins_docker(self):
        installer = open(os.path.join(_ROOT, "install.sh"), encoding="utf-8").read()
        assert "get.docker.com" not in installer
        assert " -p 8191:8191 " not in installer
        assert "flaresolverr:v3.5.2" in installer
        assert "asyncio.Semaphore(1)" in open(
            os.path.join(_ROOT, "tg/engine.py"), encoding="utf-8").read()
