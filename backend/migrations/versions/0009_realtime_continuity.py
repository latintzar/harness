"""Persist stock tracker epoch and source artifact continuity receipts."""

from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql

revision = "0009_realtime_continuity"
down_revision = "0008_realtime_modules"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(
        op.get_bind(), (Path(__file__).resolve().parents[1] / "sql/0009_realtime_continuity.sql").read_text()
    )


def downgrade() -> None:
    raise MigrationError("Retain tracker continuity receipts and roll application code back")
