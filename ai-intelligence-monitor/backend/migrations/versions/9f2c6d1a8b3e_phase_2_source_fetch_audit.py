"""phase 2 source fetch audit

Revision ID: 9f2c6d1a8b3e
Revises: 50cf7b50f3ad
Create Date: 2026-07-17 17:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f2c6d1a8b3e"
down_revision: Union[str, None] = "50cf7b50f3ad"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_fetch_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fetch_id", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=80), nullable=False),
        sa.Column("source_name", sa.String(length=240), nullable=False),
        sa.Column(
            "status",
            sa.Enum("RUNNING", "PASSED", "NOT_MODIFIED", "FAILED", name="fetchstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("request_url", sa.String(length=1000), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("etag", sa.String(length=500), nullable=True),
        sa.Column("last_modified", sa.String(length=500), nullable=True),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=False),
        sa.Column("candidates_found", sa.Integer(), nullable=False),
        sa.Column("documents_created", sa.Integer(), nullable=False),
        sa.Column("duplicates_found", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("item_results", sa.JSON(), nullable=False),
        sa.Column("artifact_path", sa.String(length=1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_source_fetch_audits_fetch_id", "source_fetch_audits", ["fetch_id"], unique=True)
    op.create_index("ix_source_fetch_audits_source_key", "source_fetch_audits", ["source_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_source_fetch_audits_source_key", table_name="source_fetch_audits")
    op.drop_index("ix_source_fetch_audits_fetch_id", table_name="source_fetch_audits")
    op.drop_table("source_fetch_audits")
