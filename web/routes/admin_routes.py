"""Admin panel routes — dashboard, users, services, logs."""

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import settings, TEMPLATES_DIR
from ..auth import get_current_admin
from ..database import get_tg_users, get_tg_user, update_tg_user, get_audit_log, audit
from ..bot_db import get_platform_stats, get_sessions, get_session_stats, get_user_config
from ..services import all_service_statuses, system_metrics, format_bytes

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["format_bytes"] = format_bytes
templates.env.globals["app_name"] = settings.app_name


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, admin: dict = Depends(get_current_admin)):
    stats = get_platform_stats()
    services = await all_service_statuses()
    metrics = system_metrics()
    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request,
        "admin": admin,
        "stats": stats,
        "services": services,
        "metrics": metrics,
    })


@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request, admin: dict = Depends(get_current_admin)):
    users = get_tg_users()
    # Enrich with session stats
    for u in users:
        u["stats"] = get_session_stats(u["user_id"])
    return templates.TemplateResponse("admin/users.html", {
        "request": request,
        "admin": admin,
        "users": users,
    })


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: int, admin: dict = Depends(get_current_admin)):
    user = get_tg_user(user_id)
    if not user:
        return RedirectResponse("/admin/users", status_code=302)
    sessions = get_sessions(user_id, limit=50)
    stats = get_session_stats(user_id)
    config = get_user_config(user_id)
    return templates.TemplateResponse("admin/user_detail.html", {
        "request": request,
        "admin": admin,
        "user": user,
        "sessions": sessions,
        "stats": stats,
        "config": config,
    })


@router.post("/users/{user_id}/tier")
async def update_tier(
    request: Request, user_id: int,
    tier: str = Form(...),
    admin: dict = Depends(get_current_admin),
):
    if tier in ("free", "trial", "premium"):
        update_tg_user(user_id, tier=tier)
        audit(admin["sub"], "update_tier", f"User {user_id} → {tier}")
    return RedirectResponse(f"/admin/users/{user_id}", status_code=302)


@router.post("/users/{user_id}/block")
async def toggle_block(request: Request, user_id: int, admin: dict = Depends(get_current_admin)):
    user = get_tg_user(user_id)
    if user:
        new_val = 0 if user["is_blocked"] else 1
        update_tg_user(user_id, is_blocked=new_val)
        action = "block_user" if new_val else "unblock_user"
        audit(admin["sub"], action, f"User {user_id}")
    return RedirectResponse(f"/admin/users/{user_id}", status_code=302)


@router.get("/services", response_class=HTMLResponse)
async def services_page(request: Request, admin: dict = Depends(get_current_admin)):
    services = await all_service_statuses()
    metrics = system_metrics()
    return templates.TemplateResponse("admin/services.html", {
        "request": request,
        "admin": admin,
        "services": services,
        "metrics": metrics,
    })


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, admin: dict = Depends(get_current_admin)):
    services_list = await all_service_statuses()
    return templates.TemplateResponse("admin/logs.html", {
        "request": request,
        "admin": admin,
        "services": services_list,
    })


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, admin: dict = Depends(get_current_admin)):
    logs = get_audit_log(200)
    return templates.TemplateResponse("admin/audit.html", {
        "request": request,
        "admin": admin,
        "logs": logs,
    })
