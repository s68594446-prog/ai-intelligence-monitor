"""phase 3b web discovery audit

Revision ID: d8b5f1a2c4e6
Revises: c7a4e2f9b1d3
Create Date: 2026-07-18 18:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8b5f1a2c4e6"
down_revision: Union[str, None] = "c7a4e2f9b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "web_discovery_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discovery_run_id", sa.String(length=64), nullable=False),
        sa.Column("replay_of", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("adapter_version", sa.String(length=80), nullable=False),
        sa.Column("query_plan_version", sa.String(length=40), nullable=False),
        sa.Column("source_policy_version", sa.String(length=40), nullable=False),
        sa.Column("coverage_id", sa.String(length=80), nullable=False),
        sa.Column("coverage_version", sa.String(length=40), nullable=False),
        sa.Column("thesis_version", sa.String(length=40), nullable=False),
        sa.Column("query_plan", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("search_results", sa.JSON(), nullable=False),
        sa.Column("triage_results", sa.JSON(), nullable=False),
        sa.Column("source_chains", sa.JSON(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("analysis_run_ids", sa.JSON(), nullable=False),
        sa.Column("failures", sa.JSON(), nullable=False),
        sa.Column("ops_check", sa.JSON(), nullable=True),
        sa.Column("artifact_paths", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discovery_run_id"),
    )
    with op.batch_alter_table("web_discovery_audits", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_web_discovery_audits_discovery_run_id"),
            ["discovery_run_id"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("web_discovery_audits", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_web_discovery_audits_discovery_run_id"))
    op.drop_table("web_discovery_audits")
