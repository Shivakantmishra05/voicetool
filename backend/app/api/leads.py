from fastapi import APIRouter, HTTPException, Query, Request

from app.middleware.admin_auth import require_admin

router = APIRouter(tags=["leads"])


@router.get("/leads")
async def list_leads(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict:
    require_admin(request)
    crm = request.app.state.crm
    return {"leads": await crm.list_leads(limit=limit)}


@router.get("/leads/{call_sid}")
async def get_lead(call_sid: str, request: Request) -> dict:
    require_admin(request)
    crm = request.app.state.crm
    lead = await crm.get_lead(call_sid)
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead
