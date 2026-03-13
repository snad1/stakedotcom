"""Authentication routes — login, logout, Telegram auth."""

from datetime import timedelta, datetime, timezone

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import settings, TEMPLATES_DIR
from ..auth import create_token, COOKIE_NAME, decode_token
from ..database import verify_admin, create_web_session, revoke_session, audit

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    admin = verify_admin(username, password)
    if not admin:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid credentials",
        }, status_code=401)

    expires = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    jti = create_web_session("admin", str(admin["id"]), expires.isoformat())
    token = create_token(
        subject=f"admin:{admin['id']}",
        role="admin",
        jti=jti,
        expires_delta=timedelta(hours=settings.jwt_expire_hours),
    )

    audit(f"admin:{username}", "login", "Admin login")
    response = RedirectResponse("/admin/", status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
        max_age=settings.jwt_expire_hours * 3600,
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        payload = decode_token(token)
        if payload and payload.get("jti"):
            revoke_session(payload["jti"])
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/auth/telegram")
async def telegram_auth(request: Request, token: str = ""):
    """Exchange a one-time Telegram token for a session cookie."""
    if not token:
        return RedirectResponse("/login", status_code=302)

    payload = decode_token(token)
    if not payload or payload.get("role") != "tg_user":
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid or expired link. Request a new one from the bot.",
        }, status_code=401)

    # Create a longer-lived session
    user_id = payload["sub"].split(":")[1]
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    jti = create_web_session("telegram", user_id, expires.isoformat())
    session_token = create_token(
        subject=payload["sub"],
        role="tg_user",
        jti=jti,
        expires_delta=timedelta(days=7),
    )

    audit(f"tg:{user_id}", "login", "Telegram user login via deep link")
    response = RedirectResponse("/dashboard/", status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        session_token,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
        max_age=7 * 86400,
    )
    return response
