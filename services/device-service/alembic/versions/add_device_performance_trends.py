"""Add device performance trends table.

Revision ID: add_device_performance_trends
Revises: add_phase_type
Create Date: 2026-03-05
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "add_device_performance_trends"
down_revision = "add_phase_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_performance_trends",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(length=50), nullable=False),
        sa.Column("bucket_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_end_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_timezone", sa.String(length=64), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("health_score", sa.Float(), nullable=True),
        sa.Column("uptime_percentage", sa.Float(), nullable=True),
        sa.Column("planned_minutes", sa.Integer(), nullable=False),
        sa.Column("effective_minutes", sa.Integer(), nullable=False),
        sa.Column("break_minutes", sa.Integer(), nullable=False),
        sa.Column("points_used", sa.Integer(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "bucket_start_utc", name="uq_perf_trend_device_bucket"),
    )

    op.create_index(
        "ix_device_performance_trends_device_id",
        "device_performance_trends",
        ["device_id"],
    )
    op.create_index(
        "ix_device_performance_trends_bucket_start_utc",
        "device_performance_trends",
        ["bucket_start_utc"],
    )
    op.create_index(
        "ix_device_performance_trends_created_at",
        "device_performance_trends",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_device_performance_trends_created_at", table_name="device_performance_trends")
    op.drop_index("ix_device_performance_trends_bucket_start_utc", table_name="device_performance_trends")
    op.drop_index("ix_device_performance_trends_device_id", table_name="device_performance_trends")
    op.drop_table("device_performance_trends")
