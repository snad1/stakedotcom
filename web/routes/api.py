"""JSON API endpoints for HTMX and programmatic access."""

import csv
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from ..auth import get_current_admin, get_current_user
from ..bot_db import (
    get_platform_stats, get_sessions, get_session, get_bets, get_bet_count,
    get_session_stats, get_user_config, discover_users, get_session_count,
    get_bets_for_chart,
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
async def api_user_sessions(
    user_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    sessions = get_sessions(user_id, limit=limit, offset=offset)
    total = get_session_count(user_id)
    return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}


@router.get("/users/{user_id}/sessions/csv")
async def api_sessions_csv(user_id: int, user: dict = Depends(get_current_user)):
    sessions = get_sessions(user_id, limit=10000, offset=0)

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["#", "Game", "Strategy", "Bets", "Wins", "Losses",
                         "Win Rate", "Profit", "Wagered", "Started", "Ended"])
        buf.seek(0)
        yield buf.getvalue()
        for s in sessions:
            buf = io.StringIO()
            writer = csv.writer(buf)
            total_bets = s.get("total_bets") or 0
            wins = s.get("wins") or 0
            win_rate = f"{wins / total_bets * 100:.1f}%" if total_bets > 0 else "—"
            writer.writerow([
                s.get("id"),
                s.get("game") or "",
                s.get("strategy") or "",
                total_bets,
                wins,
                s.get("losses") or 0,
                win_rate,
                f"{s.get('profit') or 0:.8f}",
                f"{s.get('wagered') or 0:.8f}",
                (s.get("started_at") or "")[:19],
                (s.get("ended_at") or "")[:19],
            ])
            buf.seek(0)
            yield buf.getvalue()

    headers = {"Content-Disposition": f"attachment; filename=sessions_{user_id}.csv"}
    return StreamingResponse(generate(), media_type="text/csv", headers=headers)


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


@router.get("/users/{user_id}/sessions/{session_id}/bets/csv")
async def api_bets_csv(user_id: int, session_id: int, user: dict = Depends(get_current_user)):
    bets = get_bets(user_id, session_id, limit=100000, offset=0)

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["#", "Timestamp", "Amount", "Target", "Result", "State", "Profit", "Balance"])
        buf.seek(0)
        yield buf.getvalue()
        for b in bets:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                b.get("id"),
                (b.get("timestamp") or "")[:19],
                f"{b.get('amount') or 0:.8f}",
                f"{b.get('multiplier_target') or 0:.2f}x",
                b.get("result_display") or f"{b.get('result_value') or 0:.2f}",
                b.get("state") or "",
                f"{b.get('profit') or 0:.8f}",
                f"{b.get('balance_after') or 0:.8f}",
            ])
            buf.seek(0)
            yield buf.getvalue()

    headers = {"Content-Disposition": f"attachment; filename=bets_{user_id}_session_{session_id}.csv"}
    return StreamingResponse(generate(), media_type="text/csv", headers=headers)


@router.get("/users/{user_id}/sessions/{session_id}/chart")
async def api_chart_data(user_id: int, session_id: int, user: dict = Depends(get_current_user)):
    """Return all bets for a session shaped for Lightweight Charts rendering."""
    return get_bets_for_chart(user_id, session_id)


@router.get("/audit")
async def api_audit(admin: dict = Depends(get_current_admin)):
    return get_audit_log(200)
