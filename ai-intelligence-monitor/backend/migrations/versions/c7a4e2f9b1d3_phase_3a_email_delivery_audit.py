"""phase 3a email delivery audit

Revision ID: c7a4e2f9b1d3
Revises: 9f2c6d1a8b3e
Create Date: 2026-07-18 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7a4e2f9b1d3"
down_revision: Union[str, None] = "9f2c6d1a8b3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("delivery_feedback", schema=None) as batch_op:
        batch_op.add_column(sa.Column("delivery_channel", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("recipient_fingerprint", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("provider_message_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("error_code", sa.String(length=80), nullable=True))
        batch_op.create_unique_constraint(
            "uq_delivery_feedback_idempotency_key", ["idempotency_key"]
        )
        batch_op.create_unique_constraint(
            "uq_delivery_feedback_run_channel", ["run_id", "delivery_channel"]
        )


def downgrade() -> None:
    with op.batch_alter_table("delivery_feedback", schema=None) as batch_op:
        batch_op.drop_constraint("uq_delivery_feedback_run_channel", type_="unique")
        batch_op.drop_constraint("uq_delivery_feedback_idempotency_key", type_="unique")
        batch_op.drop_column("error_code")
        batch_op.drop_column("sent_at")
        batch_op.drop_column("attempted_at")
        batch_op.drop_column("provider_message_id")
        batch_op.drop_column("recipient_fingerprint")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("delivery_channel")
