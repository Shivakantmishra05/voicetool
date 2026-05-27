from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Call, CallStatus, LeadStatus, TranscriptTurn


class CallRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def start_call(self, call_sid: str, stream_sid: str | None, caller_number: str | None) -> Call:
        existing = await self.get_by_sid(call_sid)
        if existing:
            existing.stream_sid = stream_sid or existing.stream_sid
            existing.status = CallStatus.active
            await self.session.commit()
            return existing
        call = Call(call_sid=call_sid, stream_sid=stream_sid, caller_number=caller_number, started_at=datetime.now(UTC))
        self.session.add(call)
        await self.session.commit()
        await self.session.refresh(call)
        return call

    async def get_by_sid(self, call_sid: str) -> Call | None:
        result = await self.session.execute(select(Call).where(Call.call_sid == call_sid).options(selectinload(Call.turns)))
        return result.scalar_one_or_none()

    async def add_turn(self, call: Call, speaker: str, text: str, sequence: int, confidence: float | None = None) -> None:
        self.session.add(TranscriptTurn(call_id=call.id, speaker=speaker, text=text, sequence=sequence, confidence=confidence))
        await self.session.commit()

    async def finish_call(self, call: Call, summary: dict[str, Any] | None = None, failure_reason: str | None = None) -> None:
        ended = datetime.now(UTC)
        call.ended_at = ended
        call.duration_seconds = int((ended - call.started_at).total_seconds()) if call.started_at else None
        call.status = CallStatus.failed if failure_reason else CallStatus.completed
        call.failure_reason = failure_reason
        if summary:
            call.summary = summary.get("summary")
            call.sentiment = summary.get("sentiment")
            call.outcome = summary.get("outcome")
            call.lead_info = summary.get("lead_info") or {}
            status = summary.get("lead_status")
            if status in LeadStatus._value2member_map_:
                call.lead_status = LeadStatus(status)
        await self.session.commit()

    async def dashboard_stats(self) -> dict[str, Any]:
        total = await self.session.scalar(select(func.count(Call.id)))
        active = await self.session.scalar(select(func.count(Call.id)).where(Call.status == CallStatus.active))
        booked = await self.session.scalar(select(func.count(Call.id)).where(Call.lead_status == LeadStatus.visit_booked))
        avg_duration = await self.session.scalar(select(func.avg(Call.duration_seconds)))
        converted = await self.session.scalar(
            select(func.count(Call.id)).where(Call.lead_status.in_([LeadStatus.visit_booked, LeadStatus.callback_scheduled]))
        )
        return {
            "total_calls": total or 0,
            "active_calls": active or 0,
            "booked_visits": booked or 0,
            "avg_duration": round(float(avg_duration or 0), 1),
            "lead_conversion": round(((converted or 0) / total) * 100, 1) if total else 0,
        }

    async def list_calls(self, limit: int = 50) -> list[Call]:
        result = await self.session.execute(select(Call).order_by(Call.created_at.desc()).limit(limit))
        return list(result.scalars().all())

