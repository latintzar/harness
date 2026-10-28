"""Postgres data layer (asyncpg) — local DB on the prod box.

Self-hosted Postgres on the same box as the backend: localhost, no pausing, no
egress dependency. Versioned migrations run explicitly before deployment; startup verifies
compatibility and opens pools without applying schema or data changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

import config

# Boot talks through the logger, never through print(). A print goes to stderr unformatted, so it
# skips the redacting formatter (logging_setup.py) and lands in the journal without a level — which
# is why a schema that failed to apply and an RLS wall that was never raised both read, for months,
# like ordinary boot chatter.
log = logging.getLogger("rocoto.db")

_pool: Optional[asyncpg.Pool] = None
# The employee-runtime pool — connects as the least-privilege rocoto_rt role so FORCE RLS binds it
# (SPEC §12.2). Falls back to _pool (the owner) with a LOUD boot warning until come-alive repoints it;
# ROCOTO_REQUIRE_RLS turns that fallback into a refusal to boot (_fell_back_to_owner).
_rt_pool: Optional[asyncpg.Pool] = None
# The tier-P control pool — connects as rocoto_ctrl, which SEES ALL scopes (SPEC §15.7). Used by the
# global background jobs (worker/sweep) for their CROSS-SCOPE SCANS; their per-scope WRITES still go
# through scoped_tx (rocoto_rt), so even a background job can't write outside a row's own world.
_ctrl_pool: Optional[asyncpg.Pool] = None
_runtime_startup: Optional[asyncio.Task] = None


JSONB_COLS = {"media_urls", "media_types", "transcript_meta"}

# Allowlist — column names are interpolated into SQL, so never let an unknown key through.
ALLOWED_COLS = {
    "id", "phone_number", "role", "message_text", "message_type",
    "media_urls", "media_types", "wa_message_id", "reply_to_wa_id", "reply_to_text",
    "transcript", "transcript_status", "transcript_meta", "created_at",
    # the business line the message ARRIVED ON, resolved at the door (see tenancy.py). Written by
    # both rails; read by the MEDIA path, which crosses an asyncio boundary carrying only the id.
    "tenant_id",
}


def _check_cols(cols) -> None:
    bad = [c for c in cols if c not in ALLOWED_COLS]
    if bad:
        raise ValueError(f"refusing unknown columns: {bad}")


async def _init_conn(con: asyncpg.Connection) -> None:
    await con.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    # pgvector codec — lets asyncpg send/receive `vector` columns as numpy/list.
    # Guarded: the extension may not be installed yet on a fresh box (pg_bootstrap installs it
    # before the app deploys), so we boot cleanly either way and grounding lights up once present.
    try:
        from pgvector.asyncpg import register_vector
        await register_vector(con)
    except Exception:
        pass


async def _backfill_order_tenants(con) -> None:
    """Repair NULL tenant attribution for the explicit legacy seed/reset callers.

    This DML helper remains a development compatibility seam. Ordinary process startup
    neither invokes it nor changes a column default; migrations own schema defaults.
    """
    try:
        n1 = await con.execute(
            "update orders o set tenant_id = e.t from ("
            "  select order_id, min(tenant_id) t from order_events where tenant_id is not null"
            "  group by order_id) e where e.order_id = o.id and o.tenant_id is null")
        n2 = await con.execute(
            # RUNG 2 — and it must match on more than the phone. With `customer_profiles` keyed
            # (tenant_id, phone_number), a shared customer has one row PER TENANT, so an unqualified
            # phone match would pick an arbitrary one and stamp the order with the wrong supplier —
            # then `o.tenant_id = $1` would serve it to that supplier's desk, every predicate
            # working exactly as designed. Only heal where the phone resolves to exactly ONE tenant;
            # anything ambiguous falls through to rung 3 (the deployment tenant), which is honest.
            "update orders o set tenant_id = p.tenant_id from ("
            "  select phone_number, min(tenant_id) as tenant_id from customer_profiles "
            "  where tenant_id is not null group by phone_number having count(distinct tenant_id) = 1"
            ") p where p.phone_number = o.phone_number and o.tenant_id is null")
        n3 = await con.execute(
            "update orders set tenant_id = $1 where tenant_id is null", config.BRAIN_TENANT)
        left = await con.fetchval("select count(*) from orders where tenant_id is null")
        if any(x.rsplit(" ", 1)[-1] not in ("0",) for x in (n1, n2, n3)):
            log.info("orders.tenant_id backfill: events=%s profiles=%s floor=%s; %s still null",
                     n1, n2, n3, left)
    except Exception:  # pragma: no cover — logged, never fatal
        log.exception("orders.tenant_id backfill skipped")


async def heal_order_tenants() -> None:
    """Run the legacy data backfill on demand — the seam for any writer that inserts into `orders`
    with raw SQL and cannot be changed from here (`brain/seed.py` has seven such statements and is
    owned elsewhere). A seeded row with `tenant_id IS NULL` is INVISIBLE to every tenant-filtered
    read, which would empty the demo's Pedidos tab the first time the founder pressed «reiniciar»
    after boot. Called after a seed load/reset. Idempotent; touches only NULLs."""
    if _pool is None:
        return
    async with _pool.acquire() as con:
        await _backfill_order_tenants(con)


async def init() -> None:
    """Check the deployed revision, then open the pools. Startup never migrates data."""
    from schema_migrations import assert_current

    global _pool
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=8, init=_init_conn)
    try:
        async with pool.acquire() as con:
            await assert_current(con)
        _pool = pool
        await _open_runtime_pool()
        await _open_control_pool()
    except BaseException:
        await pool.close()
        _pool = None
        raise


async def init_runtime_only(dsn: str, *, timeout_s: float = 5, max_size: int = 2) -> None:
    """Open only the restricted SDK pool, with the shared read-only schema guard.

    The owned startup task lets shutdown cancel initialization without leaving a
    late pool. This lifecycle never opens owner/control pools, migrates, seeds,
    imports the application or starts background jobs.
    """
    from schema_migrations import assert_current

    global _rt_pool, _runtime_startup
    if (_runtime_startup is not None
            or any(pool is not None for pool in (_pool, _rt_pool, _ctrl_pool))):
        raise RuntimeError("A database lifecycle is already initialized or starting")
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("Supply the commissioned restricted runtime connection")
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or not .1 <= timeout_s <= 10
            or type(max_size) is not int or not 1 <= max_size <= 4):
        raise ValueError("Use bounded runtime pool size and startup time")

    async def open_pool():
        pool = None
        try:
            async with asyncio.timeout(timeout_s):
                pool = asyncpg.create_pool(dsn, min_size=1, max_size=max_size,
                                          init=_init_conn, timeout=timeout_s, command_timeout=10)
                await pool
                async with pool.acquire() as con:
                    role = await con.fetchrow(
                        "select current_user as name,rolsuper,rolbypassrls,rolcreaterole "
                        "from pg_roles where rolname=current_user")
                    if (role['name'] != 'rocoto_rt' or role['rolsuper']
                            or role['rolbypassrls'] or role['rolcreaterole']):
                        raise RuntimeError("The product SDK requires the restricted rocoto_rt role")
                    await assert_current(con)
                return pool
        except BaseException:
            if pool is not None:
                with suppress(asyncpg.InterfaceError):
                    pool.terminate()
            raise

    task = asyncio.create_task(open_pool(), name="rocoto-runtime-pool-startup")
    _runtime_startup = task
    try:
        pool = await task
        if (_runtime_startup is not task
                or any(existing is not None for existing in (_pool, _rt_pool, _ctrl_pool))):
            pool.terminate()
            raise RuntimeError("The runtime startup was stopped or another lifecycle is already initialized")
        _rt_pool = pool
    finally:
        if _runtime_startup is task:
            _runtime_startup = None


async def _stop_runtime_startup() -> None:
    global _runtime_startup
    task, _runtime_startup = _runtime_startup, None
    if task is None:
        return
    task.cancel()
    results = await asyncio.gather(task, return_exceptions=True)
    # The opener can finish just before cancellation while its caller is still
    # queued. Dispose that result too; the caller's identity guard prevents a
    # late publication into _rt_pool.
    if isinstance(results[0], asyncpg.Pool):
        results[0].terminate()


async def close_runtime_only() -> None:
    """Drain this host's restricted pool without touching another lifecycle."""
    global _rt_pool
    if _pool is not None or _ctrl_pool is not None:
        raise RuntimeError("Use db.close for the full application lifecycle")
    await _stop_runtime_startup()
    pool, _rt_pool = _rt_pool, None
    if pool is None:
        return
    try:
        async with asyncio.timeout(5):
            await pool.close()
    except BaseException:
        pool.terminate()
        raise


async def close() -> None:
    """Release distinct pools after requests/background tasks drain, including partial startup."""
    import asyncio

    global _pool, _rt_pool, _ctrl_pool
    await _stop_runtime_startup()
    pools = {id(pool): pool for pool in (_ctrl_pool, _rt_pool, _pool) if pool is not None}
    _pool = _rt_pool = _ctrl_pool = None
    for pool in pools.values():
        try:
            await asyncio.wait_for(pool.close(), timeout=10)
        except TimeoutError:
            try:
                pool.terminate()
                log.warning("shutdown: terminated a database pool after its drain timeout")
            except Exception:
                log.exception("shutdown: database pool termination failed")
        except Exception:
            log.exception("shutdown: database pool cleanup failed")


async def _open_runtime_pool() -> None:
    """The employee-runtime pool connects as rocoto_rt so FORCE RLS actually binds it (SPEC §12.2).

    The owner pool (_pool) stays for legacy data access. When RUNTIME_DATABASE_URL is unset, or equal
    to the owner DSN, or unusable, the runtime would run AS THE OWNER — and the owner bypasses FORCE
    RLS, so every tenant wall in the database is off and only app code is holding the line. That is a
    real posture for a laptop and an unacceptable one for a box holding several houses' orders, so
    ROCOTO_REQUIRE_RLS turns each of those fallbacks into a refusal to boot."""
    global _rt_pool
    rt_dsn = config.RUNTIME_DATABASE_URL
    if rt_dsn and rt_dsn != config.DATABASE_URL:
        try:
            _rt_pool = await asyncpg.create_pool(rt_dsn, min_size=1, max_size=8, init=_init_conn)
            return
        except Exception:
            log.exception("the rocoto_rt pool could not be opened")
            _fell_back_to_owner("RUNTIME_DATABASE_URL is set but its pool could not be opened")
    else:
        _fell_back_to_owner("RUNTIME_DATABASE_URL is unset (or equals DATABASE_URL)")
    _rt_pool = _pool


def _fell_back_to_owner(why: str) -> None:
    """Say — or refuse. One place, so the two fallback roads cannot drift apart."""
    msg = (f"the employee-runtime pool would use the OWNER role ({why}), and the owner BYPASSES "
           "FORCE ROW LEVEL SECURITY: tenant isolation would be app-code-only")
    if config.REQUIRE_RLS:
        raise RuntimeError(
            msg + ". ROCOTO_REQUIRE_RLS=true, so this boot is refused. Point RUNTIME_DATABASE_URL at "
            "the rocoto_rt DSN (SPEC §12.2 / come-alive), or unset ROCOTO_REQUIRE_RLS to run without "
            "the database's own wall.")
    log.warning("%s. Set ROCOTO_REQUIRE_RLS=true to make this refuse instead.", msg)


async def _open_control_pool() -> None:
    """The tier-P control pool (rocoto_ctrl) — background jobs' cross-scope SCAN sees all scopes;
    their per-scope WRITES still go through scoped_tx (rocoto_rt). Fail-soft to the owner pool: this
    pool WIDENS what a scan sees rather than narrowing it, so the owner is not a weaker posture."""
    global _ctrl_pool
    ctrl_dsn = config._env("CONTROL_DATABASE_URL")
    if ctrl_dsn and ctrl_dsn != config.DATABASE_URL:
        try:
            _ctrl_pool = await asyncpg.create_pool(ctrl_dsn, min_size=1, max_size=4, init=_init_conn)
            return
        except Exception:  # pragma: no cover
            log.exception("the rocoto_ctrl pool could not be opened; cross-scope scans use the owner "
                          "pool")
    _ctrl_pool = _pool


async def rls_status() -> dict[str, Any]:
    """What the RUNTIME pool actually connects as — the answer /healthz publishes.

    `forced` is the whole question: True means the runtime pool is a different pool from the owner's,
    which is the only condition under which FORCE ROW LEVEL SECURITY binds anything. It reads the
    role from the database rather than from configuration, because what the box was told to connect
    as and what it is connected as are exactly the two things that drift."""
    out: dict[str, Any] = {"runtime_role": None, "forced": bool(
        _rt_pool is not None and _pool is not None and _rt_pool is not _pool)}
    pool = _rt_pool if _rt_pool is not None else _pool
    if pool is None:
        return out
    try:
        async with pool.acquire() as con:
            out["runtime_role"] = await con.fetchval("select current_user")
    except Exception as e:  # pragma: no cover — health must answer even when this one probe fails
        out["runtime_role"] = f"err: {e.__class__.__name__}"
    return out


def control_pool() -> asyncpg.Pool:
    """The tier-P (rocoto_ctrl) pool for cross-scope background scans (worker/sweep). Sees all scopes."""
    return _ctrl_pool if _ctrl_pool is not None else _pool


# public alias — callers/tests prepare a standalone connection with the same codecs the pool uses.
prepare_conn = _init_conn


def scoped_tx(scope: str):
    """Runtime scoped transaction (SPEC §12.2): acquire a rocoto_rt connection, open a txn, and set
    the RLS scope GUC (transaction-local) so the DATABASE enforces isolation on every read/write.
    All employee-runtime DB work goes through this. Usage:
        async with db.scoped_tx('t.sancho.c.34') as con:
            await spine.append_event(con, ...)
    """
    # A blank/None scope would be FAIL-OPEN: the empty ltree is an ancestor of every path, so
    # `scope <@ ''` is true for ALL rows. Refuse it here (the RLS policy also guards via nlevel>0).
    if not scope or not str(scope).strip():
        raise ValueError("scoped_tx requires a non-empty scope (a blank scope would fail-open RLS)")
    pool = _rt_pool if _rt_pool is not None else _pool
    if pool is None:
        raise RuntimeError("db.init() has not been called yet")

    class _ScopedTx:
        async def __aenter__(self):
            self._con = await pool.acquire()
            try:
                self._tx = self._con.transaction()
                await self._tx.start()
                # is_local=true → resets at commit/rollback; never leaks to the next pooled user.
                await self._con.execute("select set_config('rocoto.scope', $1, true)", scope)
            except BaseException:
                # setup failed AFTER acquire → __aexit__ never runs; release now or leak the conn
                # (a handful of leaks exhausts the pool and wedges the whole runtime).
                await pool.release(self._con)
                raise
            return self._con

        async def __aexit__(self, et, ev, tb):
            try:
                if et is None:
                    await self._tx.commit()
                else:
                    await self._tx.rollback()
            finally:
                await pool.release(self._con)

    return _ScopedTx()


def get_pool() -> asyncpg.Pool:
    """Accessor for brain/* modules that need the shared connection pool."""
    if _pool is None:
        raise RuntimeError("db.init() has not been called yet")
    return _pool


def now_iso() -> datetime:
    return datetime.now(timezone.utc)


def _ser(rec: asyncpg.Record) -> dict[str, Any]:
    d = dict(rec)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


async def insert_message(row: dict) -> dict:
    cols = list(row.keys())
    _check_cols(cols)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    q = f"insert into whatsapp_messages ({', '.join(cols)}) values ({placeholders}) returning *"
    async with _pool.acquire() as con:
        rec = await con.fetchrow(q, *[row[c] for c in cols])
        return _ser(rec)


async def update_message(msg_id: str, patch: dict) -> None:
    cols = list(patch.keys())
    _check_cols(cols)
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    q = f"update whatsapp_messages set {sets} where id = $1"
    async with _pool.acquire() as con:
        await con.execute(q, msg_id, *[patch[c] for c in cols])


async def get_messages(phone: str, after: Optional[str] = None,
                       after_id: Optional[str] = None,
                       tenant: Optional[str] = None) -> list[dict]:
    """Thread messages for `phone`, oldest-first. With `after`, only rows strictly newer than that
    timestamp. When `after_id` is also supplied, a (created_at, id) TUPLE tiebreak is used instead of
    a bare `created_at > $2` — so two rows sharing an identical microsecond timestamp no longer each
    exclude the other (the supersede double-send race, review A7): exactly one wins by id order."""
    aft: Optional[datetime] = None
    if after:
        try:
            aft = datetime.fromisoformat(after)
        except Exception:
            aft = None
    async with _pool.acquire() as con:
        if aft and after_id is not None:
            # Tuple comparison: rows after the anchor by (time, id). The anchor row itself is excluded
            # (equal tuple is not strictly greater); a same-timestamp sibling with a higher id is kept.
            recs = await con.fetch(
                "select * from whatsapp_messages where phone_number=$1 "
                "and ($4::text is null or tenant_id = $4) "
                "and (created_at, id) > ($2, $3) order by created_at asc, id asc",
                phone, aft, str(after_id), tenant,
            )
        elif aft:
            recs = await con.fetch(
                "select * from whatsapp_messages where phone_number=$1 and created_at > $2 "
                "and ($3::text is null or tenant_id = $3) "
                "order by created_at asc, id asc",
                phone, aft, tenant,
            )
        else:
            recs = await con.fetch(
                "select * from whatsapp_messages where phone_number=$1 "
                "and ($2::text is null or tenant_id = $2) order by created_at asc, id asc",
                phone, tenant,
            )
    return [_ser(r) for r in recs]


async def message_by_wamid(wamid: str) -> Optional[dict]:
    """The thread row a WhatsApp id names — {role, message_type, phone_number, age_days} — or None if
    we have never seen it. The pre-flight lookup for a reaction target (actions.send_reaction_as_rocoto):
    Meta accepts a reaction to a stale/unknown message with a 200 and refuses it later, async, on the
    status webhook (131009), so we check what we know BEFORE spending the send. Owner pool on purpose —
    whatsapp_messages is the projection, not scope-RLS'd, and the runtime role has no grant on it.
    Raises on a real DB fault (the caller distinguishes 'unknown' from 'could not check')."""
    if not wamid:
        return None
    async with _pool.acquire() as con:
        rec = await con.fetchrow(
            "select role, message_type, phone_number, created_at, "
            "       extract(epoch from (now() - created_at)) / 86400.0 as age_days "
            "from whatsapp_messages where wa_message_id = $1 order by created_at desc limit 1",
            wamid,
        )
    if rec is None:
        return None
    return {"role": rec["role"] or "", "message_type": rec["message_type"] or "text",
            "phone_number": rec["phone_number"] or "", "age_days": float(rec["age_days"] or 0.0)}


async def recent_messages_for_phone(phone: str, limit: int = 10) -> list[dict]:
    """Last `limit` messages for `phone`, returned OLDEST-FIRST, selecting only the columns the reply
    context needs (role, message_text, transcript, message_type). Bounded fetch for LLM history — the
    idx_wa_msgs_phone_time index (phone_number, created_at desc) covers the order+limit, so this never
    pulls the whole thread (review F2). Never the full `select *`."""
    n = max(int(limit), 1)
    async with _pool.acquire() as con:
        recs = await con.fetch(
            "select role, message_text, transcript, message_type from whatsapp_messages "
            "where phone_number=$1 order by created_at desc limit $2",
            phone, n,
        )
    # DB returned newest-first (index order); reverse to oldest-first for the Messages sequence.
    return [_ser(r) for r in reversed(recs)]


async def recent_messages(limit: int = 400, tenant: Optional[str] = None) -> list[dict]:
    """A1 tail: tenant-filtered — the chats roster is one business line's history, never the box's."""
    t = _tenant_or_default(tenant)
    async with _pool.acquire() as con:
        if t is None:
            recs = await con.fetch(
                "select * from whatsapp_messages order by created_at desc limit $1", limit)
        else:
            recs = await con.fetch(
                "select * from whatsapp_messages where tenant_id = $2 "
                "order by created_at desc limit $1", limit, t)
    return [_ser(r) for r in recs]


async def get_message(msg_id: str) -> Optional[dict]:
    """Single message row by id (used by finish() to avoid clobbering a 2-channel upgrade)."""
    async with _pool.acquire() as con:
        rec = await con.fetchrow("select * from whatsapp_messages where id = $1", msg_id)
    return _ser(rec) if rec else None


async def search(q: str) -> dict:
    """WhatsApp-style search: contacts (name/number) + message content (text or transcript)."""
    like = f"%{q}%"
    async with _pool.acquire() as con:
        profs = await con.fetch(
            "select phone_number, display_name from whatsapp_user_profiles "
            "where display_name ilike $1 or phone_number ilike $1 limit 20", like)
        msgs = await con.fetch(
            "select id, phone_number, message_text, transcript, message_type, created_at "
            "from whatsapp_messages where message_text ilike $1 or transcript ilike $1 "
            "order by created_at desc limit 40", like)
    import re as _re
    def _clean(s: str) -> str:
        return _re.sub(r"(?i)\bspeaker\s*\d+\s*:\s*", "", s or "").strip()
    names = {p["phone_number"]: p["display_name"] for p in profs}
    conversations = [{"phone_number": p["phone_number"], "name": p["display_name"] or ""} for p in profs]
    messages = [{
        "id": str(m["id"]), "phone_number": m["phone_number"], "name": names.get(m["phone_number"], ""),
        "snippet": _clean(m["message_text"] or m["transcript"] or ""),
        "message_type": m["message_type"] or "text",
        "created_at": (m["created_at"].isoformat() if m["created_at"] else ""),
    } for m in msgs]
    return {"conversations": conversations, "messages": messages}


async def find_call_transcript_by_sid(call_sid: str) -> Optional[dict]:
    """O(1) lookup of the call_transcript row for a Twilio CallSid — via the calls table
    (calls.id == the message id), falling back to the sid stashed in transcript_meta for rows
    whose calls row was already swept. Replaces the old 'scan the most recent 200 messages'."""
    if not call_sid:
        return None
    async with _pool.acquire() as con:
        rec = await con.fetchrow(
            "select m.* from whatsapp_messages m join calls c on c.id = m.id "
            "where c.call_sid = $1 order by m.created_at desc limit 1",
            call_sid,
        )
        if rec is None:
            rec = await con.fetchrow(
                "select * from whatsapp_messages where message_type = 'call_transcript' "
                "and transcript_meta->>'call_sid' = $1 order by created_at desc limit 1",
                call_sid,
            )
    return _ser(rec) if rec else None


async def get_pending(max_age_seconds: int = 30) -> list[dict]:
    """Rows stuck in transcript_status='pending' (e.g. process restarted mid-transcribe)."""
    async with _pool.acquire() as con:
        recs = await con.fetch(
            "select * from whatsapp_messages where transcript_status = 'pending' "
            "and created_at < now() - ($1::text || ' seconds')::interval order by created_at asc",
            str(max_age_seconds),
        )
    return [_ser(r) for r in recs]


async def profiles_map() -> dict[str, str]:
    async with _pool.acquire() as con:
        recs = await con.fetch("select phone_number, display_name from whatsapp_user_profiles")
    return {r["phone_number"]: (r["display_name"] or "") for r in recs}


async def upsert_profile(phone: str, display_name: str) -> None:
    # Keep the BUSINESS name. WhatsApp sends the sender's personal ProfileName on every inbound; it
    # must not clobber the saved business name ("Bar Lumbre") with whatever the phone's owner happens
    # to call himself. Only fill the name when we don't already have one — the operator's
    # saved/seeded business name always wins.
    async with _pool.acquire() as con:
        await con.execute(
            "insert into whatsapp_user_profiles (phone_number, display_name, updated_at) "
            "values ($1, $2, now()) "
            "on conflict (phone_number) do update set "
            "display_name = coalesce(nullif(whatsapp_user_profiles.display_name, ''), excluded.display_name), "
            "updated_at = now()",
            phone, display_name or None,
        )


async def get_elevenlabs_enabled(phone: str) -> bool:
    """B6 (SPEC §5.5): a VIEW of the one mode field — "does Rocoto answer this customer?".

    It used to read `whatsapp_user_profiles.elevenlabs_autoreply_enabled`, which after B3 chose
    nothing at all (it no longer decides who answers a call), and whose FALSE default silently
    disagreed with every other toggle. Now it reports the resolved mode: app-wide default, overridden
    per customer."""
    if not phone:
        return False
    import gate
    import runtime
    return (await gate.effective_mode(runtime.scope_of(phone)))["mode"] == gate.AUTO


async def set_elevenlabs_enabled(phone: str, enabled: bool) -> bool:
    """B6: writes THE mode (see get_elevenlabs_enabled). Returns the resolved value back."""
    import gate
    import runtime
    v = await gate.set_mode(runtime.scope_of(phone), gate.AUTO if enabled else gate.SOMBRA)
    return v["mode"] == gate.AUTO


async def get_customer_settings(phone: str) -> dict:
    """Read the operator-facing settings for one customer. B6 (SPEC §5.5): the reply switch is the
    resolved MODE (with `mode`/`mode_source` alongside, so a surface can say WHY a customer is in
    sombra — app-wide vs this customer); the profile row supplies only name + auto-windows."""
    import gate
    import runtime
    async with _pool.acquire() as con:
        rec = await con.fetchrow(
            "select phone_number, display_name, auto_windows "
            "from whatsapp_user_profiles where phone_number = $1",
            phone,
        )
    v = await gate.effective_mode(runtime.scope_of(phone))
    return {
        "phone_number": (rec and rec["phone_number"]) or phone,
        "display_name": (rec and rec["display_name"]) or "",
        "elevenlabs_autoreply_enabled": v["mode"] == gate.AUTO,
        "mode": v["mode"], "mode_source": v["source"], "mode_own": v["own"], "mode_app": v["app"],
        "auto_windows": (rec and rec["auto_windows"]) or [],
    }


async def set_auto_windows(phone: str, windows: list) -> list:
    """Set the per-customer auto-answer windows (upserts the profile row). Returns them back."""
    async with _pool.acquire() as con:
        await con.execute(
            "insert into whatsapp_user_profiles (phone_number, auto_windows, updated_at) "
            "values ($1, $2, now()) "
            "on conflict (phone_number) do update set "
            "auto_windows = excluded.auto_windows, updated_at = now()",
            phone, windows,
        )
    return windows


def in_auto_window(windows: list, now=None) -> bool:
    """True if `now` falls inside any window (empty windows = always). Handles past-midnight wrap."""
    if not windows:
        return True
    import datetime as _dt
    now = now or _dt.datetime.now()
    dow = now.weekday()                 # 0 = Mon .. 6 = Sun
    minute = now.hour * 60 + now.minute
    for w in windows:
        days = w.get("days") or list(range(7))
        if dow not in days:
            continue
        start, end = int(w.get("start", 0)), int(w.get("end", 1440))
        if start <= end:
            if start <= minute < end:
                return True
        else:                           # wraps midnight, e.g. 23:00–04:00
            if minute >= start or minute < end:
                return True
    return False


async def register_token(token: str, platform: str = "android") -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "insert into device_tokens (token, platform, updated_at) values ($1, $2, now()) "
            "on conflict (token) do update set updated_at = now()",
            token, platform,
        )


async def get_tokens() -> list[str]:
    async with _pool.acquire() as con:
        recs = await con.fetch("select token from device_tokens")
    return [r["token"] for r in recs]


async def delete_token(token: str) -> None:
    async with _pool.acquire() as con:
        await con.execute("delete from device_tokens where token = $1", token)


# ──────────────────────────── Call lifecycle ────────────────────────────
# A live call's transient state (active/ended, mode, who's handling it, the growing
# live transcript). The final, polished transcript still lands on the whatsapp_messages
# `call_transcript` row as before — this table is purely for the in-progress spectate/
# take-over UX and is non-destructive to the message log.

_CALL_COLS = {
    "id", "phone_number", "status", "mode", "operator_state", "spectatable",
    "call_sid", "recording_sid", "recording_url", "lines", "subtitle",
    "started_at", "ended_at", "updated_at",
    "order_draft", "location",  # Stage 7: live in-call order card + caller location
}


async def call_start(call_id: str, phone: str, mode: str, spectatable: bool,
                     call_sid: str = "") -> None:
    """Open (or re-open, idempotent) a live-call row."""
    async with _pool.acquire() as con:
        await con.execute(
            "insert into calls (id, phone_number, status, mode, operator_state, "
            "spectatable, call_sid, lines, subtitle, started_at, updated_at) "
            "values ($1, $2, 'active', $3, 'agent', $4, $5, '[]'::jsonb, '', now(), now()) "
            "on conflict (id) do update set status='active', mode=excluded.mode, "
            "spectatable=excluded.spectatable, call_sid=excluded.call_sid, updated_at=now()",
            call_id, phone, mode, bool(spectatable), call_sid or None,
        )


async def call_update(call_id: str, patch: dict) -> None:
    cols = [c for c in patch if c in _CALL_COLS]
    if not cols:
        return
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    q = f"update calls set {sets}, updated_at = now() where id = $1"
    async with _pool.acquire() as con:
        await con.execute(q, call_id, *[patch[c] for c in cols])


async def call_set_lines(call_id: str, lines: list[dict], subtitle: str = "") -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "update calls set lines = $2, subtitle = $3, updated_at = now() where id = $1",
            call_id, lines, subtitle,
        )


async def call_takeover(call_id: str) -> dict:
    """Operator joins: flip operator_state to 'takeover'. Returns the row (or {} if gone)."""
    async with _pool.acquire() as con:
        rec = await con.fetchrow(
            "update calls set operator_state='takeover', updated_at=now() "
            "where id=$1 and status='active' returning *",
            call_id,
        )
    return _ser(rec) if rec else {}


async def call_end(call_id: str) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "update calls set status='ended', ended_at=now(), updated_at=now() where id=$1",
            call_id,
        )


async def call_get(call_id: str) -> dict:
    async with _pool.acquire() as con:
        rec = await con.fetchrow("select * from calls where id = $1", call_id)
    return _ser(rec) if rec else {}


async def call_active() -> list[dict]:
    """Currently-active calls (newest first) — the app polls this to show 'call in progress'."""
    async with _pool.acquire() as con:
        recs = await con.fetch(
            "select * from calls where status = 'active' order by started_at desc"
        )
    return [_ser(r) for r in recs]


async def call_sweep_orphans(max_age_seconds: int = 7200) -> int:
    """Mark stale 'active' calls as ended — a restart mid-call would otherwise leave a
    ghost 'call in progress' banner forever. Returns how many were swept."""
    async with _pool.acquire() as con:
        res = await con.execute(
            "update calls set status='ended', ended_at=now(), updated_at=now() "
            "where status='active' and started_at < now() - ($1::text || ' seconds')::interval",
            str(max_age_seconds),
        )
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


# ──────────────────────────── Orders (Phase A — ungrounded → Excel) ────────────────────────────
# `lines` jsonb = [{raw_text,item,qty,unit,price,note,confidence}]. The global jsonb codec
# (set in _init_conn) encodes/decodes Python lists/dicts transparently.

_ORDER_COLS = {
    "id", "phone_number", "customer_name", "source_message_ids", "status",
    "lines", "note", "created_at", "updated_at", "dispatched_at", "delivery_note",
    "delivery_date", "tenant_id",
}

# THE ORDERS TENANT CONTRACT (2026-07-27). Every read below takes `tenant` and DEFAULTS IT TO
# `config.BRAIN_TENANT` rather than to "no predicate" — the same shape `brain/queue.py` has always
# had. Two consequences, both deliberate:
#   · a box with one tenant behaves EXACTLY as it did before (every row backfills to BRAIN_TENANT),
#     which is what keeps Peter's demo alive;
#   · a caller that FORGETS the tenant gets its own rows, never everyone's — a missed call site
#     under-serves instead of leaking, which is the safe direction to fail in.
# `ALL_TENANTS` is the explicit opt-out for the genuinely cross-tenant surfaces (the Control Room,
# ops counts). It has to be typed out, so it can never be reached by forgetting something.
ALL_TENANTS = "*"


def _tenant_or_default(tenant: Optional[str]) -> Optional[str]:
    """None → the single-tenant fallback; ALL_TENANTS → None (meaning: no predicate)."""
    if tenant == ALL_TENANTS:
        return None
    return tenant or config.BRAIN_TENANT


async def order_insert(order: dict) -> dict:
    # NEVER write a NULL tenant: a hole in the column is what makes a later predicate lie. A caller
    # that does not know its tenant is single-tenant by definition, so the fallback is correct.
    order = {**order, "tenant_id": order.get("tenant_id") or config.BRAIN_TENANT}
    cols = [c for c in order.keys() if c in _ORDER_COLS]  # allowlist — never interpolate unknowns
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    q = f"insert into orders ({', '.join(cols)}) values ({placeholders}) returning *"
    async with _pool.acquire() as con:
        rec = await con.fetchrow(q, *[order[c] for c in cols])
    return _ser(rec)


async def order_get(order_id: str, tenant: Optional[str] = None) -> Optional[dict]:
    t = _tenant_or_default(tenant)
    async with _pool.acquire() as con:
        if t is None:
            rec = await con.fetchrow("select * from orders where id = $1", order_id)
        else:
            rec = await con.fetchrow("select * from orders where id = $1 and tenant_id = $2",
                                     order_id, t)
    return _ser(rec) if rec else None


async def order_list(status: Optional[str] = None, limit: int = 200,
                     tenant: Optional[str] = None, day: Optional[str] = None) -> list[dict]:
    """`day` (YYYY-MM-DD) narrows to ONE delivery day, server-side.

    WHY IT HAD TO EXIST (2026-08-19 audit). The desk fetched an unfiltered newest-first list capped
    at 200 and filtered by day in the browser. Raquel takes 500 orders a MORNING — so the day she
    was looking at could sit entirely beyond the cap, and her desk would show an empty or
    half-empty day with no error and nothing to click. That is «las órdenes no estaban apareciendo»
    with a second, independent cause from the catalogue-gate one already fixed.

    The Excel export has always been day-scoped in SQL (sup_orders_excel), which is exactly why a
    download could contain orders the screen above it did not show — the two disagreed about the
    same day."""
    t = _tenant_or_default(tenant)
    where = ["true"]
    args: list[Any] = []
    if t is not None:
        args.append(t)
        where.append(f"o.tenant_id = ${len(args)}")
    if status:
        args.append(status)
        where.append(f"o.status = ${len(args)}")
    if day:
        # the desk's own definition of «this day»: an explicit delivery_date, else the day it came
        # in — the same coalesce the Excel route uses, so screen and download cannot disagree
        args.append(day)
        where.append("coalesce(o.delivery_date, to_char(o.created_at,'YYYY-MM-DD')) = "
                     f"${len(args)}")
    args.append(limit)
    async with _pool.acquire() as con:
        # LEFT JOIN customer_profiles for lat/lon — the Orders screen's delivery-point map (Peter,
        # 2026-07-28: "why isn't there the map at the top") plots exactly what this returns; a
        # customer with no known location contributes no pin rather than a guessed one.
        recs = await con.fetch(
            f"select o.*, p.lat as customer_lat, p.lon as customer_lon from orders o "
            f"left join customer_profiles p on p.phone_number = o.phone_number "
            f"and p.tenant_id = o.tenant_id where {' and '.join(where)} "
            f"order by o.created_at desc limit ${len(args)}", *args)
    return [_ser(r) for r in recs]


async def orders_storefront(status: Optional[str] = None, limit: int = 200,
                            tenant: Optional[str] = None) -> list[dict]:
    """E-commerce-source orders: the storefront-originated channel (basket → submit → outbox). Filters
    the projection on `from_storefront` so the supplier's e-commerce orders view sees only its channel."""
    t = _tenant_or_default(tenant)
    where = ["from_storefront"]
    args: list[Any] = []
    if t is not None:
        args.append(t)
        where.append(f"tenant_id = ${len(args)}")
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    args.append(limit)
    async with _pool.acquire() as con:
        recs = await con.fetch(
            f"select * from orders where {' and '.join(where)} "
            f"order by created_at desc limit ${len(args)}", *args)
    return [_ser(r) for r in recs]


async def order_set_status(order_id: str, status: str,
                           delivery_note: Optional[str] = None) -> Optional[dict]:
    async with _pool.acquire() as con:
        rec = await con.fetchrow(
            "update orders set status=$2, delivery_note=coalesce($3, delivery_note), "
            "updated_at=now() where id=$1 returning *",
            order_id, status, delivery_note)
    return _ser(rec) if rec else None


async def order_set_lines(order_id: str, lines: list) -> Optional[dict]:
    async with _pool.acquire() as con:
        rec = await con.fetchrow(
            "update orders set lines=$2, updated_at=now() where id=$1 returning *",
            order_id, lines)
    return _ser(rec) if rec else None


async def orders_for_dispatch(tenant: Optional[str] = None) -> list[dict]:
    """Approved orders not yet dispatched — the batch a scheduled/triggered send compiles.
    Tenant-scoped: an Excel batch that mixed two distributors' orders is one file, sent to one
    supplier, containing another supplier's customers."""
    t = _tenant_or_default(tenant)
    async with _pool.acquire() as con:
        if t is None:
            recs = await con.fetch(
                "select * from orders where status='approved' and dispatched_at is null "
                "order by created_at asc")
        else:
            recs = await con.fetch(
                "select * from orders where status='approved' and dispatched_at is null "
                "and tenant_id=$1 order by created_at asc", t)
    return [_ser(r) for r in recs]


async def orders_pending_review_count(tenant: Optional[str] = None) -> int:
    """M1M2 WP-0: the unreviewed set right now — what the pre-cutoff review-nudge reflex senses, and
    what a dispatch HOLDS (only approved ever dispatches — unchanged) + flags in the daily email."""
    t = _tenant_or_default(tenant)
    async with _pool.acquire() as con:
        if t is None:
            return int(await con.fetchval(
                "select count(*) from orders where status='needs_review'") or 0)
        return int(await con.fetchval(
            "select count(*) from orders where status='needs_review' and tenant_id=$1", t) or 0)


# ── WP-3 (their codes): the customer's ERP client code — customer_profiles.erp_client_code ──

# THE CODE IS THE SUPPLIER'S, NOT THE CUSTOMER'S. Two distributors serving the same restaurant
# each file it under their OWN ERP code, so every one of these keys on (tenant, phone). They used
# to key on phone alone, which — once `customer_profiles` held one row per phone box-wide — meant
# one supplier could read another's ERP code straight off `/orders/excel`. (A1 §7.2.)

async def erp_code_set(phone: str, code: Optional[str], *, tenant: Optional[str] = None) -> Optional[str]:
    """Set (or clear, code=None/'') the customer's client code in the supplier's ERP. Upserts the
    profile row so a code can land before the first computed profile."""
    code = (code or "").strip() or None
    t = _tenant_or_default(tenant) or config.BRAIN_TENANT
    async with _pool.acquire() as con:
        await con.execute(
            "insert into customer_profiles (phone_number, tenant_id, erp_client_code, updated_at) "
            "values ($1, $2, $3, now()) "
            "on conflict (tenant_id, phone_number) do update set "
            "erp_client_code = excluded.erp_client_code, updated_at = now()",
            phone, t, code)
    return code


async def erp_code_get(phone: str, *, tenant: Optional[str] = None) -> Optional[str]:
    t = _tenant_or_default(tenant) or config.BRAIN_TENANT
    async with _pool.acquire() as con:
        return await con.fetchval(
            "select erp_client_code from customer_profiles where phone_number=$1 and tenant_id=$2",
            phone, t)


async def erp_codes_for(phones: list[str], *, tenant: Optional[str] = None) -> dict[str, str]:
    """phone → erp_client_code for the dispatch batch (one query; only rows that HAVE a code).

    A customer shared with another supplier now yields NO code rather than the other supplier's —
    which surfaces as the existing fail-soft blank `codigo_cliente`, and is the correct answer."""
    phones = [p for p in dict.fromkeys(phones) if p]
    if not phones:
        return {}
    t = _tenant_or_default(tenant) or config.BRAIN_TENANT
    async with _pool.acquire() as con:
        rows = await con.fetch(
            "select phone_number, erp_client_code from customer_profiles "
            "where phone_number = any($1::text[]) and erp_client_code is not null "
            "and tenant_id = $2", phones, t)
    return {r["phone_number"]: r["erp_client_code"] for r in rows}


async def orders_mark_sent(order_ids: list[str]) -> None:
    if not order_ids:
        return
    async with _pool.acquire() as con:
        await con.execute(
            "update orders set status='sent', dispatched_at=now(), updated_at=now() "
            "where id = any($1::text[])", order_ids)


# ── M1M2 WP-4 (eng F1): the dispatch-time value snapshot — SET-ONCE, the meter's only source ──

async def order_set_value_snapshot(order_id: str, cents: int, detail: dict) -> bool:
    """Freeze the resolved euro value onto the order row AT dispatch. SET-ONCE (write only where
    null): a re-dispatch/backfill can fill a hole but never rewrite a snapshot the meter may have
    already billed. Returns True if this call wrote the snapshot."""
    async with _pool.acquire() as con:
        won = await con.fetchval(
            "update orders set value_snapshot_cents=$2, value_snapshot=$3, updated_at=now() "
            "where id=$1 and value_snapshot_cents is null returning id",
            order_id, int(cents), detail or {})
    return won is not None


async def order_count_since_dispatch(tenant: Optional[str] = None) -> int:
    """How many approved-unsent orders are waiting — drives the every_n cadence."""
    t = _tenant_or_default(tenant)
    async with _pool.acquire() as con:
        if t is None:
            val = await con.fetchval(
                "select count(*) from orders where status='approved' and dispatched_at is null")
        else:
            val = await con.fetchval(
                "select count(*) from orders where status='approved' and dispatched_at is null "
                "and tenant_id=$1", t)
    return int(val or 0)


# ──────────────────────────── Agent settings (dispatch cadence etc.) ────────────────────────────

async def settings_get(agent_id: str) -> dict:
    async with _pool.acquire() as con:
        val = await con.fetchval("select settings from agent_settings where agent_id=$1", agent_id)
    return val or {}


async def settings_set(agent_id: str, settings: dict) -> dict:
    async with _pool.acquire() as con:
        await con.execute(
            "insert into agent_settings (agent_id, settings, updated_at) values ($1, $2, now()) "
            "on conflict (agent_id) do update set settings=excluded.settings, updated_at=now()",
            agent_id, settings)
    return settings


# ──────────── Employee subscriptions (OUR SaaS billing of the supplier; brain/stripe_subs.py) ────────────

async def subs_row(tenant_id: str) -> Optional[dict]:
    """The supplier's billing row (Stripe customer id + Carmen's 0.3% flag), or None if never created."""
    async with _pool.acquire() as con:
        r = await con.fetchrow("select * from employee_subscriptions where tenant_id=$1", tenant_id)
    return _ser(r) if r else None


async def subs_get_customer_id(tenant_id: str) -> Optional[str]:
    async with _pool.acquire() as con:
        return await con.fetchval(
            "select stripe_customer_id from employee_subscriptions where tenant_id=$1", tenant_id)


async def subs_set_customer_id(tenant_id: str, customer_id: str) -> None:
    """Upsert the platform-side Stripe Customer id (cus_…) the supplier's subscriptions hang off."""
    async with _pool.acquire() as con:
        await con.execute(
            "insert into employee_subscriptions (tenant_id, stripe_customer_id, updated_at) "
            "values ($1, $2, now()) "
            "on conflict (tenant_id) do update set "
            "  stripe_customer_id=excluded.stripe_customer_id, updated_at=now()",
            tenant_id, customer_id)


async def subs_set_ecommerce_enabled(tenant_id: str, on: bool) -> dict:
    """Toggle Carmen's 0.3% application-fee enable. Stamps `ecommerce_enabled_at` only on the OFF→ON
    transition (the one-month lock-in clock starts then; flipping OFF leaves the timestamp for the lock-in
    check). Returns the persisted row."""
    async with _pool.acquire() as con:
        if on:
            await con.execute(
                "insert into employee_subscriptions (tenant_id, ecommerce_enabled, ecommerce_enabled_at, updated_at) "
                "values ($1, true, now(), now()) "
                "on conflict (tenant_id) do update set "
                "  ecommerce_enabled=true, "
                "  ecommerce_enabled_at=coalesce(employee_subscriptions.ecommerce_enabled_at, now()), "
                "  updated_at=now()",
                tenant_id)
        else:
            await con.execute(
                "insert into employee_subscriptions (tenant_id, ecommerce_enabled, updated_at) "
                "values ($1, false, now()) "
                "on conflict (tenant_id) do update set "
                "  ecommerce_enabled=false, updated_at=now()",
                tenant_id)
        r = await con.fetchrow("select * from employee_subscriptions where tenant_id=$1", tenant_id)
    return _ser(r)


# ─────────────────────────── Landing demo leads ───────────────────────────
async def demo_lead_insert(row: dict) -> None:
    async with _pool.acquire() as con:
        await con.execute(
            "insert into demo_leads (id, token, channel, phone, locale, ip, user_agent, "
            "consent, status, call_sid) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
            "on conflict (id) do nothing",
            row["id"], row["token"], row.get("channel", "call"), row["phone"],
            row.get("locale", "es"), row.get("ip"), row.get("user_agent"),
            bool(row.get("consent")), row.get("status", "requested"), row.get("call_sid"),
        )


_DEMO_COLS = {"status", "call_sid", "name", "business", "summary", "answers",
              "cta_text", "wa_text", "lines", "ended_at"}


async def demo_lead_update(token: str, patch: dict) -> None:
    cols = [c for c in patch if c in _DEMO_COLS]
    if not cols:
        return
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    async with _pool.acquire() as con:
        await con.execute(
            f"update demo_leads set {sets} where token = $1",
            token, *[patch[c] for c in cols],
        )


async def demo_lead_get(token: str) -> dict | None:
    async with _pool.acquire() as con:
        r = await con.fetchrow("select * from demo_leads where token = $1", token)
    return _ser(r) if r else None


async def demo_leads_count_since(hours: int) -> int:
    async with _pool.acquire() as con:
        return int(await con.fetchval(
            "select count(*) from demo_leads where created_at > now() - ($1 || ' hours')::interval",
            str(hours),
        ) or 0)


async def demo_lead_count_for_phone(phone: str, hours: int) -> int:
    async with _pool.acquire() as con:
        return int(await con.fetchval(
            "select count(*) from demo_leads where phone = $1 and "
            "created_at > now() - ($2 || ' hours')::interval",
            phone, str(hours),
        ) or 0)
