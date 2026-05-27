from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_session

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("select 1"))
    diagnostics = getattr(request.app.state, "startup_diagnostics", None)
    if diagnostics and request.app.state.settings.startup_provider_checks_required and not diagnostics.ready:
        return {"status": "degraded"}
    return {"status": "ready"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    return request.app.state.metrics.prometheus()


@router.get("/startup-diagnostics")
async def startup_diagnostics(request: Request) -> dict:
    return request.app.state.startup_diagnostics.public_dict()
