"""Freeze the existing maintained schema and isolation policies as the adoption floor."""
import os
from pathlib import Path

from alembic import context, op
from sqlalchemy import text

from schema_migrations import MigrationError, execute_sql, tenant_slug

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None
backward_compatible = False
SQL = Path(__file__).resolve().parents[1] / "sql"
SOURCES = ("schema", "brain_schema", "shop_schema", "onboard_schema", "playbook_schema",
           "channel_schema", "runtime", "rls")


def upgrade() -> None:
    if context.is_offline_mode():
        raise MigrationError("Baseline requires a blank database check; use schema_migrations plan")
    connection = op.get_bind()
    tables = connection.execute(text(
        "select tablename from pg_tables where schemaname='public' "
        "and tablename not in ('alembic_version','schema_compatibility')"
    )).all()
    if tables:
        raise MigrationError("Unversioned existing tables require schema_migrations adopt")
    execute_sql(connection, "create extension if not exists ltree; create extension if not exists vector;")
    for name in SOURCES:
        execute_sql(connection, (SQL / f"0001_{name}.sql").read_text())
    tenant = tenant_slug(context.config.attributes.get("tenant", os.environ.get("BRAIN_TENANT", "rocoto")))
    execute_sql(connection, f"alter table orders alter column tenant_id set default '{tenant}'")


def downgrade() -> None:
    raise MigrationError("The adopted baseline is the rollback floor; restore a backup to replace its data")
