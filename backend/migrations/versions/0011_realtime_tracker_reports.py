"""Retain historical tracker reports independently of observed activity evidence."""

from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql

revision = "0011_realtime_tracker_reports"
down_revision = "0010_realtime_configuration"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(
        op.get_bind(),
        (Path(__file__).resolve().parents[1] / "sql/0011_realtime_tracker_reports.sql").read_text(),
    )


def downgrade() -> None:
    raise MigrationError("Retain reported tracker history and roll application code back")
