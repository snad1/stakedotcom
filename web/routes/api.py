"""JSON API endpoints for HTMX and programmatic access."""

from fastapi import APIRouter, Depends, Query

from ..auth import get_current_admin, get_current_user
from ..bot_db import (
    get_platform_stats, get_sessions, get_session, get_bets, get_bet_count,
    get_session_stats, get_user_config, discover_users,
)
from ..database import get_tg_users, get_audit_log
from ..services import all_service_statuses, system_metrics, restart_service, stop_service, run_update, SERVICES

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/metrics")
async def metrics(admin: dict = Depends(get_current_admin)):
    return system_metrics()


@router.get("/services")
async def services(admin: dict = Depends(get_current_admin)):
    return await all_service_statuses()


@router.post("/services/{label}/restart")
async def api_restart(label: str, admin: dict = Depends(get_current_admin)):
    name = SERVICES.get(label)
    if not name:
        return {"error": f"Unknown service: {label}"}
    from ..database import audit
    audit(admin["sub"], "restart_service", name)
    return await restart_service(name)


@router.post("/services/{label}/stop")
async def api_stop(label: str, admin: dict = Depends(get_current_admin)):
    name = SERVICES.get(label)
    if not name:
        return {"error": f"Unknown service: {label}"}
    from ..database import audit
    audit(admin["sub"], "stop_service", name)
    return await stop_service(name)


@router.post("/services/update")
async def api_update(admin: dict = Depends(get_current_admin)):
    from ..database import audit
    audit(admin["sub"], "run_update", "Git pull + restart")
    return await run_update()


@router.get("/stats")
async def platform_stats(admin: dict = Depends(get_current_admin)):
    return get_platform_stats()


@router.get("/users")
async def api_users(admin: dict = Depends(get_current_admin)):
    users = get_tg_users()
    for u in users:
        u["stats"] = get_session_stats(u["user_id"])
    return users


@router.get("/users/{user_id}/sessions")
async def api_user_sessions(user_id: int, user: dict = Depends(get_current_user)):
    return get_sessions(user_id, limit=50)


@router.get("/users/{user_id}/sessions/{session_id}")
async def api_session_detail(user_id: int, session_id: int, user: dict = Depends(get_current_user)):
    return get_session(user_id, session_id)


@router.get("/users/{user_id}/sessions/{session_id}/bets")
async def api_bets(
    user_id: int, session_id: int,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    bets = get_bets(user_id, session_id, limit=limit, offset=offset)
    total = get_bet_count(user_id, session_id)
    return {"bets": bets, "total": total, "limit": limit, "offset": offset}


@router.get("/audit")
async def api_audit(admin: dict = Depends(get_current_admin)):
    return get_audit_log(200)
