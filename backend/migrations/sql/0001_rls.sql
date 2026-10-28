-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- Rocoto ISOLATION FLOOR — Row-Level Security + role split (SPEC §12.2, §15.7, BUILD-PLAN 1.6).
-- ═══════════════════════════════════════════════════════════════════════════════════════════
-- "A bug in our query code still can't cross tenants, because the database itself refuses."
--
-- Applies ONLY to the NEW scope-keyed runtime tables (the legacy tables the live service uses are
-- untouched, so the running app is unaffected). Defense-in-depth UNDER the app-level ltree filter.
--
-- THE ROLE SPLIT (SPEC §15.7 build-note: never BYPASSRLS; tier-P is a ROLE, not a spoofable claim):
--   * rocoto_rt   — the tenant runtime. Least-privilege, NOT owner/superuser → can NEVER bypass RLS.
--                   It only ever sees `scope <@ current_setting('rocoto.scope')` (set per txn by
--                   scoped_tx). It CANNOT become rocoto_ctrl, so it cannot self-escalate to tier-P.
--   * rocoto_ctrl — the Control-Room / tier-P role. Sees ALL scopes (DB-enforced by current_user),
--                   still fully audited because every write it makes is an override.* / approval event.
--   Both are NOLOGIN here; come-alive gives them LOGIN + vaulted passwords (NOT in this file).
--
-- POLICY (every scope-keyed runtime table, ENABLE + FORCE so even the owner obeys):
--   visible/writable IFF  current_user = 'rocoto_ctrl'          (tier-P, un-spoofable)
--                    OR   row.scope <@ current_setting('rocoto.scope')   (the actor's subtree)
--   Unset GUC → NULL → fail-CLOSED (a connection with no scope set sees nothing).
--
-- Idempotent. No secrets committed.
-- ═══════════════════════════════════════════════════════════════════════════════════════════

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'rocoto_rt')   then create role rocoto_rt   nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'rocoto_ctrl') then create role rocoto_ctrl nologin; end if;
end $$;

do $$
declare
  t text;
  tables text[] := array[
    'event','contact','contact_role','fact','card','summary','open_loop','timer',
    'pending_turn','action','scope_state','scope_node','sweep_state','wake','assembly_snapshot',
    'manifest_sense','manifest_tool','manifest_stakeholder','manifest_duty','manifest_presence',
    'standing_instruction'
  ];
begin
  foreach t in array tables loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format('drop policy if exists %I on %I', t || '_scope', t);
    -- nlevel(...) > 0 closes BOTH the unset GUC (NULL → NULL>0 → NULL → invisible) AND the
    -- empty-string case ('' → nlevel 0 → 0>0 false → invisible). The empty ltree is an ancestor of
    -- EVERY path, so without this guard a blank scope would make `scope <@ ''` true for all rows.
    execute format(
      'create policy %I on %I '
      'using ( current_user = ''rocoto_ctrl'' '
      '        or ( nlevel(current_setting(''rocoto.scope'', true)::ltree) > 0 '
      '             and scope <@ current_setting(''rocoto.scope'', true)::ltree ) ) '
      'with check ( current_user = ''rocoto_ctrl'' '
      '        or ( nlevel(current_setting(''rocoto.scope'', true)::ltree) > 0 '
      '             and scope <@ current_setting(''rocoto.scope'', true)::ltree ) )',
      t || '_scope', t);
    -- the tenant runtime gets full DML; tier-P (rocoto_ctrl) gets SELECT-all here, and its WRITES
    -- are restricted below to the override/approval surface (SPEC §15.7: ctrl inserts only override.*
    -- / approval events, never direct writes to derived memory).
    execute format('grant select, insert, update, delete on %I to rocoto_rt', t);
    execute format('grant select on %I to rocoto_ctrl', t);
  end loop;
end $$;

-- tier-P WRITE surface (SPEC §15.7): ONLY event (override.*) + action (approvals). No INSERT grant on
-- fact/card/summary/open_loop/... so a buggy/compromised ctrl path CANNOT fabricate memory or forge facts.
grant insert, update on event, action to rocoto_ctrl;

-- and a trigger fences ctrl's event inserts to override.* types (approvals ride the action table).
create or replace function enforce_ctrl_event_write() returns trigger as $$
begin
  if current_user = 'rocoto_ctrl' and NEW.type not like 'override.%' then
    raise exception 'tier-P (rocoto_ctrl) may only append override.* events (SPEC §15.7), got type=%', NEW.type;
  end if;
  return NEW;
end;
$$ language plpgsql;
drop trigger if exists trg_ctrl_event_write on event;
create trigger trg_ctrl_event_write before insert or update on event
  for each row execute function enforce_ctrl_event_write();

-- identity/mapping plumbing queried PRE-scope (endpoint/entity_map are HOW you find the scope) — no
-- scope-RLS; reached only through scope-bounded joins. rt writes, ctrl reads.
grant select, insert, update, delete on endpoint, entity_map to rocoto_rt;
grant select on endpoint, entity_map to rocoto_ctrl;
-- global ops tables (no per-party scope). alert + heartbeat (WP-0.5) are ops surfaces, not derived
-- memory, so ctrl gets full DML here (it resolves alerts + the worker stamps heartbeat via the ctrl pool).
grant select, insert, update, delete on dead_letter, spine_dualwrite_state, manifest_reflex, alert, heartbeat to rocoto_rt;
grant select on dead_letter, spine_dualwrite_state, manifest_reflex to rocoto_ctrl;
grant select, insert, update, delete on alert, heartbeat to rocoto_ctrl;

-- Pedidos billing + metering (PEDIDOS-PRICING-BUILD.md §3/§5). Keyed by tenant_id, NOT by an ltree
-- scope, so these belong here rather than in the scope-RLS array above — every read is already
-- tenant-bounded by the resolved session, never by a client-supplied id. rt writes (the webhook
-- provisions and the meter counts); ctrl reads, so the Control Room can see what someone is paying
-- and how much of their band they have used without being able to rewrite either.
grant select, insert, update, delete on pedidos_subscription, pedidos_order_usage, pedidos_webhook_events to rocoto_rt;
grant select on pedidos_subscription, pedidos_order_usage, pedidos_webhook_events to rocoto_ctrl;

grant usage on schema public to rocoto_rt, rocoto_ctrl;
grant usage, select on all sequences in schema public to rocoto_rt, rocoto_ctrl;
