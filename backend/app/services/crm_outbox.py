import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from app.database.session import SessionLocal
from app.models import CRMOutboxEvent, CRMOutboxStatus
from app.observability import Metrics
from app.services.supabase_crm import SupabaseCRM
from app.utils.logging import log


class CRMOutbox:
    def __init__(self, crm: SupabaseCRM, metrics: Metrics | None = None, poll_seconds: float = 5.0, batch_size: int = 10):
        self.crm = crm
        self.metrics = metrics
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self._stopping = asyncio.Event()

    async def enqueue(self, payload: dict[str, Any]) -> str:
        event = CRMOutboxEvent(
            call_sid=str(payload.get("call_sid") or ""),
            destination="supabase",
            payload=payload,
            status=CRMOutboxStatus.pending,
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
        async with SessionLocal() as session:
            session.add(event)
            await session.commit()
            await session.refresh(event)
        log.info("crm_outbox_enqueued", outbox_id=event.id, call_sid=event.call_sid, payload_size_bytes=len(str(payload)))
        return event.id

    async def enqueue_and_try_delivery(self, payload: dict[str, Any]) -> str:
        event_id = await self.enqueue(payload)
        await self.deliver_once(event_id=event_id)
        return event_id

    async def run_forever(self) -> None:
        log.info("crm_outbox_worker_started")
        while not self._stopping.is_set():
            try:
                await self.deliver_pending()
            except Exception as exc:
                log.exception("crm_outbox_worker_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                continue
        log.info("crm_outbox_worker_stopped")

    def stop(self) -> None:
        self._stopping.set()

    async def deliver_pending(self) -> int:
        now = datetime.now(UTC)
        async with SessionLocal() as session:
            result = await session.execute(
                select(CRMOutboxEvent)
                .where(
                    CRMOutboxEvent.status.in_([CRMOutboxStatus.pending, CRMOutboxStatus.failed]),
                    or_(CRMOutboxEvent.next_attempt_at.is_(None), CRMOutboxEvent.next_attempt_at <= now),
                )
                .order_by(CRMOutboxEvent.created_at.asc())
                .limit(self.batch_size)
            )
            events = list(result.scalars().all())
        delivered = 0
        for event in events:
            if await self.deliver_once(event_id=event.id):
                delivered += 1
        return delivered

    async def deliver_once(self, event_id: str) -> bool:
        async with SessionLocal() as session:
            event = await session.get(CRMOutboxEvent, event_id)
            if not event or event.status == CRMOutboxStatus.delivered:
                return False
            event.attempts += 1
            event.status = CRMOutboxStatus.pending
            await session.commit()
            payload = dict(event.payload or {})
            attempts = event.attempts
            call_sid = event.call_sid

        log.info("crm_outbox_delivery_attempt", outbox_id=event_id, call_sid=call_sid, attempts=attempts)
        started = datetime.now(UTC)
        delivered = await self.crm.insert_call(payload)
        if self.metrics:
            self.metrics.observe_ms("crm_write", (datetime.now(UTC) - started).total_seconds() * 1000)

        async with SessionLocal() as session:
            event = await session.get(CRMOutboxEvent, event_id)
            if not event:
                return delivered
            if delivered:
                event.status = CRMOutboxStatus.delivered
                event.delivered_at = datetime.now(UTC)
                event.last_error = None
                log.info("crm_outbox_delivery_success", outbox_id=event_id, call_sid=call_sid, attempts=event.attempts)
            else:
                event.status = CRMOutboxStatus.failed
                event.last_error = "supabase delivery failed"
                event.next_attempt_at = datetime.now(UTC) + _backoff(event.attempts)
                if self.metrics:
                    self.metrics.inc("crm_retry_count")
                log.warning(
                    "crm_outbox_delivery_failed",
                    outbox_id=event_id,
                    call_sid=call_sid,
                    attempts=event.attempts,
                    next_attempt_at=event.next_attempt_at.isoformat(),
                )
            await session.commit()
        return delivered


def _backoff(attempts: int) -> timedelta:
    seconds = min(300, 2 ** min(max(attempts, 1), 8))
    return timedelta(seconds=seconds)
