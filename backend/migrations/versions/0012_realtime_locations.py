"""Keep a location's trackers, revisions, outputs, records, schedule and runs in PostgreSQL."""

from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql

revision = "0012_realtime_locations"
down_revision = "0011_realtime_tracker_reports"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(
        op.get_bind(),
        (Path(__file__).resolve().parents[1] / "sql/0012_realtime_locations.sql").read_text(),
    )


def downgrade() -> None:
    raise MigrationError("Retain location tracker history and roll application code back")
