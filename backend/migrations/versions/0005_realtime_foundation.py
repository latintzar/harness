"""Add the scoped real-time event inbox and transactional processing outbox."""

from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql

revision = "0005_realtime_foundation"
down_revision = "0004_module_schema"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(
        op.get_bind(), (Path(__file__).resolve().parents[1] / "sql/0005_realtime_foundation.sql").read_text()
    )


def downgrade() -> None:
    raise MigrationError("Retain admitted events and roll application code back")
