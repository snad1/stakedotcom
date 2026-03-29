"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings, TEMPLATES_DIR, STATIC_DIR
from .database import init_db
from .auth import get_optional_user
from .services import format_bytes

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (env=%s)", settings.app_name, settings.app_env)
    init_db()
    # Sync known TG users from bot data dir
    from .bot_db import discover_users
    from .database import sync_tg_user
    for uid in discover_users():
        sync_tg_user(uid)
    logger.info("Discovered %d bot users", len(discover_users()))
    yield
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Make format_bytes available in templates
templates.env.globals["format_bytes"] = format_bytes
templates.env.globals["app_name"] = settings.app_name


@app.exception_handler(HTTPException)
async def auth_exception_handler(request: Request, exc: HTTPException):
    """Redirect browsers to login on 401/403; return JSON for API calls."""
    if exc.status_code in (401, 403):
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return RedirectResponse("/login", status_code=302)
    detail = exc.detail if settings.app_env != "production" else "An error occurred. Please try again."
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error: %s", exc, exc_info=True)
    detail = str(exc) if settings.app_env != "production" else "Something went wrong. Please try again."
    return JSONResponse(status_code=500, content={"detail": detail})


@app.get("/")
async def root(request: Request):
    user = get_optional_user(request)
    if user:
        if user.get("role") == "admin":
            return RedirectResponse("/admin/", status_code=302)
        return RedirectResponse("/dashboard/", status_code=302)
    return RedirectResponse("/login", status_code=302)


# ── Register routes ──
from .routes.auth_routes import router as auth_router
from .routes.admin_routes import router as admin_router
from .routes.user_routes import router as user_router
from .routes.api import router as api_router
from .routes.ws_routes import router as ws_router

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(user_router)
app.include_router(api_router)
app.include_router(ws_router)
