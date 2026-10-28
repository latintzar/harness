"""Move optional modules' lazy schema into the explicit deployment transaction."""
from pathlib import Path

from alembic import op

from schema_migrations import MigrationError, execute_sql, validate_snapshot

revision = "0004_module_schema"
down_revision = "0003_operator_console"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(op.get_bind(), (Path(__file__).resolve().parents[1]
                               / "sql/0004_module_schema.sql").read_text())
    validate_snapshot(op.get_bind(), "0004-catalog.json")


def downgrade() -> None:
    raise MigrationError("Module tables may predate adoption; retain data and roll application code back")
