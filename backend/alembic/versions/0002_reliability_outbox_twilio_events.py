"""reliability outbox and twilio events

Revision ID: 0002_reliability_outbox
Revises: 0001_initial_schema
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_reliability_outbox"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    crm_status = postgresql.ENUM("pending", "delivered", "failed", name="crmoutboxstatus", create_type=False)
    crm_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "crm_outbox_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("call_sid", sa.String(length=80), nullable=False),
        sa.Column("destination", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", crm_status, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_crm_outbox_status_next_attempt", "crm_outbox_events", ["status", "next_attempt_at"], unique=False)
    op.create_index("ix_crm_outbox_call_sid", "crm_outbox_events", ["call_sid"], unique=False)

    op.create_table(
        "twilio_status_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("call_sid", sa.String(length=80), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("call_status", sa.String(length=80), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sequence", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_sid", "event_type", "sequence", name="uq_twilio_status_call_event_sequence"),
    )
    op.create_index("ix_twilio_status_call_sid", "twilio_status_events", ["call_sid"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_twilio_status_call_sid", table_name="twilio_status_events")
    op.drop_table("twilio_status_events")
    op.drop_index("ix_crm_outbox_call_sid", table_name="crm_outbox_events")
    op.drop_index("ix_crm_outbox_status_next_attempt", table_name="crm_outbox_events")
    op.drop_table("crm_outbox_events")
    sa.Enum(name="crmoutboxstatus").drop(op.get_bind(), checkfirst=True)
