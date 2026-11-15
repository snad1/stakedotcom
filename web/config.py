"""Application settings loaded from environment / .env file."""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pydantic import model_validator  # noqa: E402
from pydantic_settings import BaseSettings  # noqa: E402
from shared.web.settings import (  # noqa: E402
    DEFAULT_ADMIN_PASS, DEFAULT_SECRET_KEY, validate_web_credentials,
)

_env = os.getenv("APP_ENV", "production")
_env_file = ".env.testing" if _env == "testing" else ".env"


class Settings(BaseSettings):
    # ── App ──
    app_name: str = "Stake Admin"
    app_env: str = _env
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8001

    # ── Auth ──
    secret_key: str = DEFAULT_SECRET_KEY
    admin_user: str = "admin"
    admin_pass: str = DEFAULT_ADMIN_PASS
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24
    tg_token_expire_minutes: int = 5

    # ── Paths ──
    db_path: str = os.path.expanduser("~/.stake_web.db")
    bot_data_dir: str = os.path.expanduser("~/.stakebot_tg")
    install_dir: str = os.path.expanduser("~/stake-bot")
    repo_dir: str = os.path.expanduser("~/stake")

    # ── Services ──
    service_cli: str = "stake"
    service_tg: str = "stake-tg"
    service_web: str = "stake-web"

    # ── Telegram ──
    tg_bot_token: str = ""

    model_config = {"env_prefix": "STAKE_WEB_", "env_file": _env_file, "extra": "ignore"}

    @model_validator(mode="after")
    def _reject_weak_auth(self):
        validate_web_credentials(self.secret_key, self.admin_pass, "STAKE_WEB_")
        return self


settings = Settings()

# Resolved paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
