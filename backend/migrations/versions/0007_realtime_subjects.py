"""Add typed subject identities and revision-qualified event associations."""

from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql

revision = "0007_realtime_subjects"
down_revision = "0006_realtime_replay"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(
        op.get_bind(), (Path(__file__).resolve().parents[1] / "sql/0007_realtime_subjects.sql").read_text()
    )


def downgrade() -> None:
    raise MigrationError("Retain subject identities and source evidence; roll application code back")
