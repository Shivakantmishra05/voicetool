from typing import Any

from sqlalchemy.exc import IntegrityError

from app.database.session import SessionLocal
from app.models import TwilioStatusEvent
from app.utils.logging import log


async def persist_twilio_status(payload: dict[str, Any], event_type: str) -> None:
    call_sid = str(payload.get("CallSid") or payload.get("call_sid") or "")
    if not call_sid:
        log.warning("twilio_status_missing_call_sid", event_type=event_type, payload_keys=sorted(payload.keys()))
        return
    sequence = str(payload.get("SequenceNumber") or payload.get("Timestamp") or payload.get("MessageSid") or payload.get("CallStatus") or "0")
    event = TwilioStatusEvent(
        call_sid=call_sid,
        event_type=event_type,
        call_status=_text(payload.get("CallStatus") or payload.get("StreamStatus")),
        error_code=_text(payload.get("ErrorCode")),
        error_message=_text(payload.get("ErrorMessage")),
        sequence=sequence,
        payload={str(key): str(value) for key, value in payload.items()},
    )
    async with SessionLocal() as session:
        session.add(event)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            log.info("twilio_status_duplicate_ignored", call_sid=call_sid, event_type=event_type, sequence=sequence)
            return
    log.info(
        "twilio_status_persisted",
        call_sid=call_sid,
        event_type=event_type,
        call_status=event.call_status,
        error_code=event.error_code,
        sequence=sequence,
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
