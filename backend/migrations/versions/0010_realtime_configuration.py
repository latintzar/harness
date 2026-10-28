"""Maintain typed source configuration and actual activation receipts."""

from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql

revision = "0010_realtime_configuration"
down_revision = "0009_realtime_continuity"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(
        op.get_bind(),
        (Path(__file__).resolve().parents[1] / "sql/0010_realtime_configuration.sql").read_text(),
    )


def downgrade() -> None:
    raise MigrationError("Retain configuration history and roll application code back")
