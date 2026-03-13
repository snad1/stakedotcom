"""Telegram user-facing routes — dashboard, sessions, settings."""

from math import ceil

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import settings, TEMPLATES_DIR
from ..auth import get_current_tg_user
from ..bot_db import get_sessions, get_session, get_bets, get_session_stats, get_user_config, get_bet_count, get_session_count
from ..services import format_bytes

router = APIRouter(prefix="/dashboard", tags=["user"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["format_bytes"] = format_bytes
templates.env.globals["app_name"] = settings.app_name


def _user_id(payload: dict) -> int:
    return int(payload["sub"].split(":")[1])


@router.get("/", response_class=HTMLResponse)
async def user_dashboard(request: Request, user: dict = Depends(get_current_tg_user)):
    uid = _user_id(user)
    sessions = get_sessions(uid, limit=10)
    stats = get_session_stats(uid)
    return templates.TemplateResponse("user/dashboard.html", {
        "request": request,
        "user": user,
        "user_id": uid,
        "sessions": sessions,
        "stats": stats,
    })


@router.get("/sessions", response_class=HTMLResponse)
async def session_list(request: Request, page: int = Query(1, ge=1), user: dict = Depends(get_current_tg_user)):
    uid = _user_id(user)
    per_page = 20
    total_sessions = get_session_count(uid)
    total_pages = max(1, ceil(total_sessions / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    sessions = get_sessions(uid, limit=per_page, offset=offset)
    return templates.TemplateResponse("user/history.html", {
        "request": request,
        "user": user,
        "user_id": uid,
        "sessions": sessions,
        "page": page,
        "total_pages": total_pages,
        "total_sessions": total_sessions,
        "per_page": per_page,
    })


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: int, page: int = Query(1, ge=1), user: dict = Depends(get_current_tg_user)):
    uid = _user_id(user)
    session = get_session(uid, session_id)
    if not session:
        return RedirectResponse("/dashboard/sessions", status_code=302)
    per_page = 50
    bet_count = get_bet_count(uid, session_id)
    total_pages = max(1, ceil(bet_count / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    bets = get_bets(uid, session_id, limit=per_page, offset=offset)
    return templates.TemplateResponse("user/session.html", {
        "request": request,
        "user": user,
        "user_id": uid,
        "session": session,
        "bets": bets,
        "bet_count": bet_count,
        "page": page,
        "total_pages": total_pages,
        "per_page": per_page,
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: dict = Depends(get_current_tg_user)):
    uid = _user_id(user)
    config = get_user_config(uid)
    return templates.TemplateResponse("user/settings.html", {
        "request": request,
        "user": user,
        "user_id": uid,
        "config": config,
    })
