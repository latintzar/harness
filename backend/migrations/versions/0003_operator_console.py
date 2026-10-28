"""Add explicitly granted operator support operations and immutable audit receipts."""
from pathlib import Path

from alembic import op
from sqlalchemy import text

from schema_migrations import MigrationError, execute_sql

revision = "0003_operator_console"
down_revision = "0002_inbound_idempotency"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    execute_sql(op.get_bind(), (Path(__file__).resolve().parents[1]
                               / "sql/0003_operator_console.sql").read_text())


def downgrade() -> None:
    connection = op.get_bind()
    tables = ("operator_synthetic_run", "operator_request", "operator_audit", "operator_access")
    connection.execute(text("lock table " + ",".join(tables) + " in access exclusive mode"))
    for table in tables:
        if connection.execute(text(f"select exists(select 1 from {table})")).scalar():
            raise MigrationError("Operator records require retention; use application rollback")
    for table in tables:
        op.drop_table(table)
