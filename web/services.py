"""System service management — delegates to shared library."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from .config import settings
from shared.web.services import (  # noqa: E402
    service_status, restart_service, stop_service,
    stream_logs, system_metrics, format_bytes,
    make_services,
)

_svc = make_services(settings, "stake.py")
SERVICES = _svc["SERVICES"]
all_service_statuses = _svc["all_service_statuses"]
git_pull = _svc["git_pull"]
run_update = _svc["run_update"]
