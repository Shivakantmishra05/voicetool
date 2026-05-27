from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.config import Settings, get_settings
from app.telephony.security import validate_twilio_request
from app.telephony.stream_auth import StreamTokenService
from app.telephony.twilio import inbound_stream_twiml
from app.utils.logging import log

router = APIRouter(prefix="/twilio", tags=["twilio"])


@router.post("/voice")
async def inbound_voice(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    await validate_twilio_request(request, settings)
    form = await request.form()
    call_sid = str(form.get("CallSid") or "")
    if not call_sid:
        return Response("<Response><Reject reason=\"rejected\"/></Response>", media_type="application/xml", status_code=400)
    token_service: StreamTokenService = request.app.state.stream_tokens
    stream_token = token_service.issue(call_sid)
    log.info(
        "twilio_stream_token_issued",
        call_sid=call_sid,
        websocket_url_host=str(settings.public_base_url),
        websocket_path="/ws/twilio/{token}",
        token_preview=token_service.preview(stream_token),
    )
    return Response(content=inbound_stream_twiml(settings, form.get("From"), stream_token), media_type="application/xml")
