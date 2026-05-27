from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, Integer, JSON, String, Text
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

