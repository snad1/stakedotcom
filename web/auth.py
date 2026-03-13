"""JWT authentication — admin + Telegram user support."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request, HTTPException, status
from jose import JWTError, jwt

from .config import settings
from .database import is_session_valid, revoke_session

COOKIE_NAME = "stake_admin_token"


def create_token(subject: str, role: str, jti: str, expires_delta: timedelta | None = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=settings.jwt_expire_hours))
    payload = {
        "sub": subject,
        "role": role,
        "jti": jti,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
        jti = payload.get("jti")
        if jti and not is_session_valid(jti):
            return None
        return payload
    except JWTError:
        return None


def get_token_from_request(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token")


# ── FastAPI dependencies ──

def _get_payload(request: Request) -> dict:
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return payload


def get_current_admin(request: Request) -> dict:
    payload = _get_payload(request)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return payload


def get_current_tg_user(request: Request) -> dict:
    payload = _get_payload(request)
    if payload.get("role") != "tg_user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Telegram user access required")
    return payload


def get_current_user(request: Request) -> dict:
    return _get_payload(request)


def get_optional_user(request: Request) -> Optional[dict]:
    token = get_token_from_request(request)
    if not token:
        return None
    return decode_token(token)
