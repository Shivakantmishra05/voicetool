import hashlib
import hmac
import secrets
from time import time

from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import Settings

SESSION_COOKIE = "dh_admin_session"
CSRF_COOKIE = "dh_csrf"


def require_admin(request: Request) -> None:
    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(SESSION_COOKIE)
    if not _verify_session(cookie, settings):
        raise HTTPException(status_code=401, detail="authentication required")


def ensure_csrf_cookie(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)


def create_login_response(settings: Settings) -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _sign_session(settings),
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        secure=settings.environment != "local",
        samesite="lax",
    )
    return response


def clear_login_response() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def validate_login(settings: Settings, username: str, password: str) -> bool:
    expected_user = settings.admin_username or "admin"
    expected_password = settings.admin_password or "admin"
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)


def validate_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not hmac.compare_digest(cookie, csrf_token):
        raise HTTPException(status_code=403, detail="invalid csrf token")


def mask_phone(phone: str | None) -> str:
    if not phone:
        return "Unknown"
    if len(phone) <= 4:
        return "****"
    return f"{phone[:3]}****{phone[-2:]}"


def _sign_session(settings: Settings) -> str:
    exp = str(int(time() + settings.admin_session_ttl_seconds))
    sig = hmac.new(settings.resolved_admin_session_secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _verify_session(cookie: str | None, settings: Settings) -> bool:
    if not cookie or "." not in cookie:
        return False
    exp, sig = cookie.rsplit(".", 1)
    try:
        expires_at = int(exp)
    except ValueError:
        return False
    if expires_at < int(time()):
        return False
    expected = hmac.new(settings.resolved_admin_session_secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
