"""Enforce inbound webhook idempotency atomically; duplicate data requires explicit repair."""
from alembic import op
from sqlalchemy import text

from schema_migrations import MigrationError, execute_sql

revision = "0002_inbound_idempotency"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None
backward_compatible = True


def upgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(text(
        "select 1 from whatsapp_messages where wa_message_id is not null and wa_message_id <> '' "
        "group by wa_message_id having count(*) > 1 limit 1"
    )).first()
    if duplicate:
        raise MigrationError("Duplicate inbound webhook identities require a reviewed data repair")
    execute_sql(connection,
        "create unique index if not exists idx_wamsg_wa_message_id on whatsapp_messages "
        "(wa_message_id) where wa_message_id is not null and wa_message_id <> ''")
    row = connection.execute(text(
        "select i.indisunique,i.indisvalid,i.indnkeyatts,pg_get_indexdef(i.indexrelid,1,true) as key, "
        "pg_get_expr(i.indpred,i.indrelid) as predicate from pg_index i "
        "where i.indexrelid='public.idx_wamsg_wa_message_id'::regclass "
        "and i.indrelid='public.whatsapp_messages'::regclass"
    )).first()
    if (row is None or not row.indisunique or not row.indisvalid or row.indnkeyatts != 1
            or row.key != 'wa_message_id'
            or row.predicate != "((wa_message_id IS NOT NULL) AND (wa_message_id <> ''::text))"):
        raise MigrationError("Existing webhook index differs from the required unique identity index")


def downgrade() -> None:
    op.drop_index("idx_wamsg_wa_message_id", table_name="whatsapp_messages")
