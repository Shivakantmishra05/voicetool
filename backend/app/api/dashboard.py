import csv
import io

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session
from app.middleware.admin_auth import clear_login_response, create_login_response, ensure_csrf_cookie, mask_phone, require_admin, validate_csrf, validate_login
from app.services.call_repository import CallRepository

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["mask_phone"] = mask_phone


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    require_admin(request)
    repo = CallRepository(session)
    return templates.TemplateResponse("dashboard.html", {"request": request, "stats": await repo.dashboard_stats(), "calls": await repo.list_calls()})


@router.get("/calls/export.csv")
async def export_calls_secure(request: Request, session: AsyncSession = Depends(get_session)):
    require_admin(request)
    repo = CallRepository(session)
    calls = await repo.list_calls(limit=1000)

    async def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["call_sid", "caller", "status", "lead_status", "duration_seconds", "sentiment", "outcome", "created_at"])
        yield buffer.getvalue()
        for call in calls:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                [
                    call.call_sid,
                    mask_phone(call.caller_number),
                    call.status.value,
                    call.lead_status.value,
                    call.duration_seconds or 0,
                    call.sentiment or "",
                    call.outcome or "",
                    call.created_at,
                ]
            )
            yield buffer.getvalue()

    return StreamingResponse(rows(), media_type="text/csv", headers={"content-disposition": "attachment; filename=calls.csv"})


@router.get("/calls.csv")
async def legacy_export_redirect(request: Request):
    require_admin(request)
    return RedirectResponse("/calls/export.csv", status_code=307)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    csrf_token = ensure_csrf_cookie(request)
    response = templates.TemplateResponse("login.html", {"request": request, "csrf_token": csrf_token})
    response.set_cookie("dh_csrf", csrf_token, httponly=True, secure=request.app.state.settings.environment != "local", samesite="lax")
    return response


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    if not validate_login(request.app.state.settings, username, password):
        return RedirectResponse("/login", status_code=303)
    return create_login_response(request.app.state.settings)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    validate_csrf(request, csrf_token)
    return clear_login_response()
