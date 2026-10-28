"""Persist immutable replay plans and atomic, source-scoped admission cursors."""

from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql

revision = "0006_realtime_replay"
down_revision = "0005_realtime_foundation"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(
        op.get_bind(), (Path(__file__).resolve().parents[1] / "sql/0006_realtime_replay.sql").read_text()
    )


def downgrade() -> None:
    raise MigrationError("Retain replay plans and committed cursors; roll application code back")
