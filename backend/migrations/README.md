# Database migrations

Alembic owns PostgreSQL schema changes. Application startup and module `ensure_schema` helpers
verify the deployed revision; they perform ordinary reads, never DDL or a data backfill.
The [public architecture](../../docs/ARCHITECTURE.md) describes this optional backend closure. Database connections belong to the operator's selected PostgreSQL installation.

From `backend/`, with the selected environment's owner connection in `DATABASE_URL`:

```sh
python -m schema_migrations plan
python -m schema_migrations preflight
python -m schema_migrations upgrade head
python -m schema_migrations check
```

`plan` lists the checked-out revision chain without connecting. `preflight` reads the selected database
and returns JSON: current/target revision, pending revisions, `action` (`upgrade`, `adopt`, `check`)
and `backward_compatible`. Deployment uses this result before cutover. Connections and passwords
stay in environment configuration; diagnostics contain schema identities, never URLs or SQL parameters.
Alembic itself is also available with `python -m alembic -c alembic.ini …` for revision authoring.

## Existing databases

An empty public schema can upgrade directly. A database with unversioned application tables requires
an explicit, validated adoption:

```sh
python -m schema_migrations verify-legacy
python -m schema_migrations adopt head
```

The first command reads metadata only. Adoption compares existing columns, types, constraints,
indexes, policies, grants, triggers and functions against [the frozen baseline](baseline-catalog.json),
checks the runtime/control role split, then stamps `0001_baseline` and upgrades in the same transaction.
It never replays the old bootstrap SQL against existing customer data. A mismatch names the affected
objects and requires a reviewed forward repair. Extra application objects can remain; required objects
and grants must retain the reviewed shape. Existing tenant defaults and customer rows are preserved.

Revision `0004` also validates the [optional-module schema](0004-catalog.json) after bringing the old
lazy tables under migrations. Existing objects cannot silently defeat an `IF NOT EXISTS` declaration.
These catalog files are metadata snapshots generated from fresh PostgreSQL 16 databases at the named
revision. The baseline test regenerates and compares its snapshot. They contain no customer data.

## Change and rollback

Create a revision with `python -m alembic -c alembic.ini revision -m 'describe the shape change'`.
Write explicit `upgrade()` and `downgrade()` operations, including a data-preserving rollback or a
clear refusal when reversal would lose records. Add a PostgreSQL test for existing data and the
intended rollback. Applied revision scripts and their SQL remain immutable; fixes get a new revision.
The original `schema_runtime.sql` and `schema_rls.sql` are historical runtime-only test references.
New database changes belong in revisions, not those old snapshots or application modules.

Each revision declares `backward_compatible`. It defaults to false. Set true only for an expansion
that preserves the previous application's schema contract. Migration commits update `alembic_version`
and the supported ancestor revisions in `schema_compatibility` atomically. An older binary can start
against the expanded schema only when that metadata explicitly includes its required revision. A
contracting revision truncates the compatibility list. Its release needs a coordinated cutover, not
an automatic rollback promise. Production rollback normally restores code and keeps compatible data.

Migration commands serialize on a PostgreSQL advisory transaction lock, with bounded lock and statement
waits. All revisions, data changes and revision bookkeeping in one invocation commit together. A failure
rolls the transaction back. Ordinary app/worker startup is read-only and safely runs concurrently.

`0001` is the adoption floor: downgrading below it requires a backup restore. `0002` makes inbound
webhook deduplication strict and is reversible. It refuses duplicate pre-existing identities for
explicit repair. `0003` can remove its new operator tables only while they are empty; retained audit
records prohibit destructive downgrade. `0004` preserves optional-module tables that may have existed
before adoption and therefore refuses automatic removal. These refusals preserve data during rollback.

Fresh installation's `orders.tenant_id` default uses explicit `--tenant` or `BRAIN_TENANT` (default
`rocoto`). Existing adoption preserves its prior default. Explicit legacy seed/reset helpers can still
repair their own NULL tenant rows using DML; boot no longer backfills customer records.

## Verification

The original migration suite uses `tools/test-database.py`, `tests/test_schema_migrations.py` and `tests/test_rls_wall.py` in its owning backend source. Those test drivers belong to the separate backend closure. The public [lean checks](../../README.md#verify-locally) verify the reusable workspace and portable bare artifact. Commission PostgreSQL and validate its migration suite before using this optional data path.

The original disposable-Compose suite covers fresh/concurrent upgrades, repeated upgrades, read-only boot, adoption with existing data, schema/grant drift, transaction rollback, operator audit preservation and revision compatibility.


Revision `0005_realtime_foundation` adds Rocoto Real-time sources, immutable input/profile/event
records, quarantine and a leased transactional outbox under the `realtime` schema. Its company
bindings reference the existing tenant registry. The previous application schema is preserved;
identity import is a separate explicit operation, and application rollback retains admitted data.
See [the Real-time package](../realtime/README.md) for permission and resource boundaries.

Revision `0006_realtime_replay` adds immutable replay plans, durable cursors and bounded database
clocks. Plans retain source grants and company budgets. Events and their outbox/cursor advancement
commit atomically through the shared admission service. Rollback retains admitted replay history.

Revision `0007_realtime_subjects` adds typed source-backed identity revisions and exact event binding
sets. Replay copies retain original grant dependencies. `0008_realtime_modules` adds versioned core
module projections and includes their retained bytes in the company ledger. Both are additive;
rollback preserves the admitted evidence and restores compatible application code.

Revision `0012_realtime_locations` adds a location's tracker tables: identity and cameras, three-part
tracker revisions, the current output with its ledger, retail records, the run schedule and the newest
runs. It is additive and joins the same company byte ledger; rollback restores application code and
keeps the rows. [The Real-time package](../realtime/README.md#location-trackers) lists the tables.
