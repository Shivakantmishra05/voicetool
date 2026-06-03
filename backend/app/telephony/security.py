from fastapi import HTTPException, Request
from twilio.request_validator import RequestValidator

from app.config import Settings


async def validate_twilio_request(request: Request, settings: Settings) -> None:
    if settings.environment == "local":
        return
    signature = request.headers.get("x-twilio-signature")
    if not signature:
        raise HTTPException(status_code=403, detail="missing Twilio signature")
    form = await request.form()
    validator = RequestValidator(settings.twilio_auth_token)
    url = _public_request_url(request, settings)
    if not validator.validate(url, dict(form), signature):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")


def _public_request_url(request: Request, settings: Settings) -> str:
    base = str(settings.public_base_url).rstrip("/")
    path = request.url.path
    query = request.url.query
    return f"{base}{path}" + (f"?{query}" if query else "")
