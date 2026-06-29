from time import perf_counter

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.config import Settings, get_settings
from app.telephony.security import validate_twilio_request
from app.telephony.stream_auth import StreamTokenService
from app.telephony.twilio import inbound_stream_twiml
from app.services.twilio_status import persist_twilio_status
from app.utils.logging import log

router = APIRouter(prefix="/twilio", tags=["twilio"])


@router.post("/voice")
async def inbound_voice(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    started = perf_counter()
    await validate_twilio_request(request, settings)
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    if not call_sid:
        return Response("<Response><Reject reason=\"rejected\"/></Response>", media_type="application/xml", status_code=400)
    customer_name = (
        request.query_params.get("customer_name")
        or request.query_params.get("CustomerName")
        or request.query_params.get("name")
        or form.get("customer_name")
        or form.get("CustomerName")
        or form.get("Name")
    )
    token_service: StreamTokenService = request.app.state.stream_tokens
    stream_token = token_service.issue(call_sid)
    log.info(
        "twilio_stream_token_issued",
        call_sid=call_sid,
        websocket_url_host=str(settings.public_base_url),
        websocket_path="/ws/twilio/{token}",
        token_preview=token_service.preview(stream_token),
    )
    request.app.state.metrics.observe_ms("twilio_webhook_latency", (perf_counter() - started) * 1000)
    return Response(
        content=inbound_stream_twiml(settings, form.get("From"), stream_token, str(customer_name or "").strip()),
        media_type="application/xml",
    )


@router.post("/status")
async def call_status(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    started = perf_counter()
    await validate_twilio_request(request, settings)
    form = await request.form()
    await persist_twilio_status(dict(form), "call_status")
    request.app.state.metrics.observe_ms("twilio_status_callback_latency", (perf_counter() - started) * 1000)
    return Response(status_code=204)


@router.post("/stream-status")
async def stream_status(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    started = perf_counter()
    await validate_twilio_request(request, settings)
    form = await request.form()
    await persist_twilio_status(dict(form), "stream_status")
    request.app.state.metrics.observe_ms("twilio_stream_status_callback_latency", (perf_counter() - started) * 1000)
    return Response(status_code=204)
