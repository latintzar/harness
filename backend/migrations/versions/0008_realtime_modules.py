"""Persist bounded, revision-qualified core module projections."""

from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql

revision = "0008_realtime_modules"
down_revision = "0007_realtime_subjects"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(
        op.get_bind(), (Path(__file__).resolve().parents[1] / "sql/0008_realtime_modules.sql").read_text()
    )


def downgrade() -> None:
    raise MigrationError("Retain core module projections and roll application code back")
