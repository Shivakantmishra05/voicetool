"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    call_status = postgresql.ENUM("active", "completed", "failed", name="callstatus", create_type=False)
    lead_status = postgresql.ENUM(
        "new",
        "qualified",
        "visit_booked",
        "callback_scheduled",
        "not_interested",
        "needs_follow_up",
        name="leadstatus",
        create_type=False,
    )
    call_status.create(op.get_bind(), checkfirst=True)
    lead_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("call_sid", sa.String(length=80), nullable=False),
        sa.Column("stream_sid", sa.String(length=80), nullable=True),
        sa.Column("caller_number", sa.String(length=32), nullable=True),
        sa.Column("status", call_status, nullable=False),
        sa.Column("lead_status", lead_status, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("sentiment", sa.String(length=40), nullable=True),
        sa.Column("outcome", sa.String(length=120), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("lead_info", sa.JSON(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_calls_call_sid", "calls", ["call_sid"], unique=True)
    op.create_index(op.f("ix_calls_caller_number"), "calls", ["caller_number"], unique=False)

    op.create_table(
        "transcript_turns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("call_id", sa.String(length=36), nullable=False),
        sa.Column("speaker", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_turns_call_created", "transcript_turns", ["call_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_turns_call_created", table_name="transcript_turns")
    op.drop_table("transcript_turns")
    op.drop_index(op.f("ix_calls_caller_number"), table_name="calls")
    op.drop_index("ix_calls_call_sid", table_name="calls")
    op.drop_table("calls")
    sa.Enum(name="leadstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="callstatus").drop(op.get_bind(), checkfirst=True)
