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
    url = str(request.url)
    if not validator.validate(url, dict(form), signature):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")

