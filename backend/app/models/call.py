from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin


class CallStatus(str, Enum):
    active = "active"
    completed = "completed"
    failed = "failed"


class LeadStatus(str, Enum):
    new = "new"
    qualified = "qualified"
    visit_booked = "visit_booked"
    callback_scheduled = "callback_scheduled"
    not_interested = "not_interested"
    needs_follow_up = "needs_follow_up"


class Call(Base, TimestampMixin):
    __tablename__ = "calls"
    __table_args__ = (Index("ix_calls_call_sid", "call_sid", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    call_sid: Mapped[str] = mapped_column(String(80), nullable=False)
    stream_sid: Mapped[str | None] = mapped_column(String(80))
    caller_number: Mapped[str | None] = mapped_column(String(32), index=True)
    status: Mapped[CallStatus] = mapped_column(SAEnum(CallStatus), default=CallStatus.active)
    lead_status: Mapped[LeadStatus] = mapped_column(SAEnum(LeadStatus), default=LeadStatus.new)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    sentiment: Mapped[str | None] = mapped_column(String(40))
    outcome: Mapped[str | None] = mapped_column(String(120))
    summary: Mapped[str | None] = mapped_column(Text)
    lead_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    turns: Mapped[list["TranscriptTurn"]] = relationship(back_populates="call", cascade="all, delete-orphan")


class TranscriptTurn(Base, TimestampMixin):
    __tablename__ = "transcript_turns"
    __table_args__ = (Index("ix_turns_call_created", "call_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    speaker: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column()
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    call: Mapped[Call] = relationship(back_populates="turns")


class CRMOutboxStatus(str, Enum):
    pending = "pending"
    delivered = "delivered"
    failed = "failed"


class CRMOutboxEvent(Base, TimestampMixin):
    __tablename__ = "crm_outbox_events"
    __table_args__ = (
        Index("ix_crm_outbox_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_crm_outbox_call_sid", "call_sid"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    call_sid: Mapped[str] = mapped_column(String(80), nullable=False)
    destination: Mapped[str] = mapped_column(String(40), nullable=False, default="supabase")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[CRMOutboxStatus] = mapped_column(SAEnum(CRMOutboxStatus), default=CRMOutboxStatus.pending, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class TwilioStatusEvent(Base, TimestampMixin):
    __tablename__ = "twilio_status_events"
    __table_args__ = (
        Index("ix_twilio_status_call_sid", "call_sid"),
        UniqueConstraint("call_sid", "event_type", "sequence", name="uq_twilio_status_call_event_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    call_sid: Mapped[str] = mapped_column(String(80), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    call_status: Mapped[str | None] = mapped_column(String(80))
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    sequence: Mapped[str] = mapped_column(String(80), nullable=False, default="0")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
