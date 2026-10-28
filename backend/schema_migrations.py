"""Explicit Alembic lifecycle for the maintained backend's PostgreSQL schema.

DATABASE_URL selects the owner connection. App startup calls assert_current only;
operators run upgrade or validated adopt separately. SQL sources are immutable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import asyncpg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import make_url, pool, text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parent
LOCK_ID = 73682740609432
BASELINE = "0001_baseline"


class MigrationError(RuntimeError):
    """An actionable diagnostic containing object identities, never connection secrets."""


def configuration() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def head_revision() -> str:
    head = ScriptDirectory.from_config(configuration()).get_current_head()
    if not head:
        raise MigrationError("The checkout must contain one Alembic head")
    return head


def sqlalchemy_url(dsn: str):
    url = make_url(dsn)
    if url.drivername not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise MigrationError("Migrations require a PostgreSQL owner connection")
    return url.set(drivername="postgresql+asyncpg")


def migration_engine(dsn: str):
    """Use the same native DSN parser as app startup, including TLS and server options."""
    url = sqlalchemy_url(dsn)
    native_dsn = "postgresql:" + dsn.split(":", 1)[1]

    async def connect():
        return await asyncpg.connect(native_dsn)

    return create_async_engine(url, async_creator=connect, poolclass=pool.NullPool, echo=False)


async def assert_current(connection) -> None:
    """Read-only startup guard, including blank, older and unknown/future databases."""
    if not await connection.fetchval("select to_regclass('public.alembic_version')"):
        raise MigrationError("Database is unversioned; run schema_migrations upgrade or validated adopt")
    revisions = await connection.fetch("select version_num from public.alembic_version")
    actual = [row["version_num"] for row in revisions]
    expected = head_revision()
    if actual == [expected]:
        return
    if len(actual) == 1 and await connection.fetchval(
        "select to_regclass('public.schema_compatibility')"
    ) and await connection.fetchval(
        "select compatible_revisions ? $1 from public.schema_compatibility where version_num=$2",
        expected, actual[0],
    ):
        return
    raise MigrationError("Database revision differs from this release; run schema_migrations check")


def execute_sql(connection, sql: str) -> None:
    """Run reviewed PostgreSQL scripts intact (functions and DO blocks contain semicolons)."""
    connection.connection.dbapi_connection.run_async(lambda driver: driver.execute(sql))


def tenant_slug(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise MigrationError("--tenant or BRAIN_TENANT must name the reviewed legacy tenant slug")
    return value


CATALOG = {
    "grants": """select table_name || '.' || grantee || '.' || privilege_type as name,
        to_jsonb(is_grantable) as value from information_schema.role_table_grants
        where table_schema='public' and grantee in ('rocoto_rt','rocoto_ctrl')
        and table_name not in ('alembic_version','schema_compatibility')""",
    "columns": """select c.relname || '.' || a.attname as name,
        jsonb_build_array(format_type(a.atttypid,a.atttypmod),a.attnotnull,
            case when c.relname='orders' and a.attname='tenant_id' then '<deployment-tenant>'
                 else pg_get_expr(d.adbin,d.adrelid) end) as value
        from pg_attribute a join pg_class c on c.oid=a.attrelid
        join pg_namespace n on n.oid=c.relnamespace
        left join pg_attrdef d on d.adrelid=a.attrelid and d.adnum=a.attnum
        where n.nspname='public' and c.relkind in ('r','p') and a.attnum>0 and not a.attisdropped
        and c.relname not in ('alembic_version','schema_compatibility')""",
    "constraints": """select c.relname || '.' || x.conname as name,
        to_jsonb(pg_get_constraintdef(x.oid)) as value from pg_constraint x
        join pg_class c on c.oid=x.conrelid join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='public' and c.relname not in ('alembic_version','schema_compatibility')""",
    "indexes": """select indexname as name,to_jsonb(indexdef) as value
        from pg_indexes where schemaname='public'
        and tablename not in ('alembic_version','schema_compatibility')
        and indexname <> 'idx_wamsg_wa_message_id'""",
    "policies": """select tablename || '.' || policyname as name,
        jsonb_build_array(permissive,roles,cmd,qual,with_check) as value
        from pg_policies where schemaname='public'""",
    "rls": """select relname as name,jsonb_build_array(relrowsecurity,relforcerowsecurity) as value
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='public' and relkind in ('r','p')
        and relname not in ('alembic_version','schema_compatibility')""",
    "triggers": """select c.relname || '.' || t.tgname as name,
        jsonb_build_array(pg_get_triggerdef(t.oid),t.tgenabled) as value
        from pg_trigger t join pg_class c on c.oid=t.tgrelid
        join pg_namespace n on n.oid=c.relnamespace
        where n.nspname='public' and not t.tgisinternal""",
    "functions": """select p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' as name,
        to_jsonb(pg_get_functiondef(p.oid)) as value
        from pg_proc p join pg_namespace n on n.oid=p.pronamespace
        where n.nspname='public' and p.prokind='f'
        and not exists (select 1 from pg_depend d where d.objid=p.oid
                        and d.classid='pg_proc'::regclass and d.deptype='e')""",
}


def catalog(connection) -> dict[str, dict[str, Any]]:
    return {kind: {row.name: row.value for row in connection.execute(text(sql))}
            for kind, sql in CATALOG.items()}


def validate_snapshot(connection, filename: str) -> None:
    expected = json.loads((ROOT / "migrations" / filename).read_text())
    actual = catalog(connection)
    differences = [f"{kind}:{name}" for kind, items in expected.items()
                   for name, value in items.items() if actual[kind].get(name) != value]
    expected_tables = set(expected["rls"])
    differences.extend("grants:" + name for name in actual["grants"]
                       if name.split(".")[0] in expected_tables and name not in expected["grants"])
    if differences:
        raise MigrationError("Legacy baseline differs; review a forward repair before adoption: "
                             + ", ".join(differences[:20]))


def validate_baseline(connection) -> None:
    validate_snapshot(connection, "baseline-catalog.json")
    default = connection.execute(text("select pg_get_expr(d.adbin,d.adrelid) "
        "from pg_attrdef d join pg_attribute a on a.attrelid=d.adrelid and a.attnum=d.adnum "
        "where a.attrelid='public.orders'::regclass and a.attname='tenant_id'")).scalar()
    if not isinstance(default, str) or not re.fullmatch(r"'[A-Za-z0-9_-]+'::text", default):
        raise MigrationError("Legacy orders need a reviewed tenant default before adoption")
    if connection.execute(text("select exists(select 1 from orders where tenant_id is null)")).scalar():
        raise MigrationError("Legacy orders need explicit tenant attribution repair before adoption")
    validate_roles(connection)


def validate_roles(connection) -> None:
    for role in ("rocoto_rt", "rocoto_ctrl"):
        row = connection.execute(text(
            "select rolsuper,rolbypassrls,rolcreaterole from pg_roles where rolname=:role"),
            {"role": role}).first()
        if row is None or row.rolsuper or row.rolbypassrls or row.rolcreaterole:
            raise MigrationError("Legacy runtime/control role isolation differs from the baseline")
    if connection.execute(text(
        "select pg_has_role('rocoto_rt','rocoto_ctrl','MEMBER')")).scalar():
        raise MigrationError("Runtime role can become control role; repair grants before adoption")


def record_compatibility(connection) -> None:
    """Persist reviewed expand/contract support for N-1 application rollback."""
    current = connection.execute(text("select version_num from public.alembic_version")).scalar_one_or_none()
    if current is None:
        return
    validate_roles(connection)
    scripts = ScriptDirectory.from_config(configuration())
    supported = []
    for item in scripts.walk_revisions(base="base", head=current):
        supported.append(item.revision)
        if not getattr(item.module, "backward_compatible", False):
            break
    execute_sql(connection, "create table if not exists public.schema_compatibility "
                "(singleton boolean primary key default true check(singleton), "
                "version_num text not null, compatible_revisions jsonb not null)")
    connection.execute(text("insert into public.schema_compatibility values(true,:revision,"
        "cast(:supported as jsonb)) on conflict(singleton) do update set "
        "version_num=excluded.version_num,compatible_revisions=excluded.compatible_revisions"),
        {"revision": current, "supported": json.dumps(supported)})
    execute_sql(connection, "grant select on public.alembic_version,public.schema_compatibility "
                "to rocoto_rt,rocoto_ctrl")


async def preflight(dsn: str) -> dict:
    """Read the actual target and expose adoption/upgrade and code rollback compatibility."""
    engine = migration_engine(dsn)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            exists = (await connection.execute(text(
                "select to_regclass('public.alembic_version')"))).scalar()
            current = (await connection.execute(text(
                "select version_num from public.alembic_version"))).scalar_one_or_none() if exists else None
            tables = (await connection.execute(text("select count(*) from pg_tables "
                "where schemaname='public' and tablename not in ('alembic_version','schema_compatibility')"
            ))).scalar()
            action = "upgrade"
            if current is None and tables:
                await connection.run_sync(validate_baseline)
                action = "adopt"
            scripts = ScriptDirectory.from_config(configuration())
            pending = list(scripts.iterate_revisions(
                head_revision(), current or (BASELINE if tables else "base")))
            compatible = all(getattr(item.module, "backward_compatible", False)
                             for item in pending if item.revision != BASELINE)
            if current == head_revision():
                action = "check"
            return {"ok": True, "current": current, "target": head_revision(), "action": action,
                    "backward_compatible": compatible,
                    "pending": [item.revision for item in reversed(pending)]}
    finally:
        await engine.dispose()


def _apply(connection, operation: str, revision: str, tenant: str) -> None:
    cfg = configuration()
    cfg.attributes.update(connection=connection, tenant=tenant)
    if operation == "adopt":
        version = connection.execute(text("select to_regclass('public.alembic_version')")).scalar()
        if version and connection.execute(text("select count(*) from public.alembic_version")).scalar():
            raise MigrationError("Database already has a migration revision; use upgrade")
        validate_baseline(connection)
        # Adoption records verified existing structure. It executes zero baseline DDL/data SQL.
        command.stamp(cfg, BASELINE)
        command.upgrade(cfg, revision)
    elif operation == "verify-legacy":
        validate_baseline(connection)
    elif operation == "upgrade":
        command.upgrade(cfg, revision)
    elif operation == "downgrade":
        command.downgrade(cfg, revision)
    else:
        raise MigrationError("Unknown schema operation")


async def migrate(dsn: str, operation: str = "upgrade", revision: str = "head",
                  tenant: str = "rocoto") -> None:
    """Explicit async entrypoint for owned test bootstrap and the deployment command."""
    tenant = tenant_slug(tenant)
    engine = migration_engine(dsn)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            await connection.execute(text("SET LOCAL statement_timeout = '5min'"))
            await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
            await connection.run_sync(_apply, operation, revision, tenant)
    finally:
        await engine.dispose()


async def check(dsn: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await assert_current(connection)
    finally:
        await connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=(
        "upgrade", "downgrade", "adopt", "verify-legacy", "preflight", "check", "plan"))
    parser.add_argument("revision", nargs="?", default="head")
    parser.add_argument("--tenant", default=os.environ.get("BRAIN_TENANT", "rocoto"))
    args = parser.parse_args()
    try:
        if args.operation == "plan":
            scripts = ScriptDirectory.from_config(configuration())
            print(json.dumps({"head": head_revision(), "revisions": [
                {"revision": item.revision, "parent": item.down_revision, "description": item.doc}
                for item in reversed(list(scripts.walk_revisions()))]}))
            return 0
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            raise MigrationError("DATABASE_URL must select the migration owner connection")
        if args.operation == "preflight":
            print(json.dumps(asyncio.run(preflight(dsn))))
            return 0
        if args.operation == "check":
            asyncio.run(check(dsn))
        else:
            asyncio.run(migrate(dsn, args.operation, args.revision, args.tenant))
        print(json.dumps({"ok": True, "operation": args.operation, "revision": args.revision}))
        return 0
    except Exception as error:
        # SQLAlchemy exceptions can contain URLs/SQL parameters. Report safe object diagnostics only.
        detail = str(error) if isinstance(error, MigrationError) else type(error).__name__
        print(json.dumps({"ok": False, "operation": args.operation, "error": detail}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
