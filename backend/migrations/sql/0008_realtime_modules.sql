-- Durable, source-scoped snapshots produced by maintained core modules.
-- A projection is append-only: a changed input manifest produces a new row.
create table realtime.module_projection (
 tenant_id text not null,
 projection_id bigint generated always as identity,
 source_id text not null,
 source_asset_id text not null check(length(source_asset_id) between 1 and 256),
 original_source_id text not null check(length(original_source_id) between 1 and 256),
 lane text not null check(lane in ('live','replay')),
 module_revision text not null check(length(module_revision) between 1 and 256),
 playhead_s numeric check(playhead_s is null or playhead_s >= 0),
 digest text not null check(digest ~ '^[0-9a-f]{64}$'),
 input_manifest jsonb not null check(octet_length(input_manifest::text)<=16777216),
 snapshot jsonb not null check(octet_length(snapshot::text)<=16777216),
 freshness jsonb not null check(octet_length(freshness::text)<=8192),
 replay_contexts jsonb not null default '[]'::jsonb check(octet_length(replay_contexts::text)<=65536),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 created_at timestamptz not null default now(),
 primary key(tenant_id,projection_id),
 unique(tenant_id,digest),
 foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);
create index realtime_module_projection_lookup on realtime.module_projection(
 tenant_id,source_id,source_asset_id,module_revision,lane,playhead_s,projection_id desc);

alter table realtime.module_projection enable row level security;
alter table realtime.module_projection force row level security;
create policy module_projection_read on realtime.module_projection for select
 using(realtime.source_visible(tenant_id,source_id));
create policy module_projection_insert on realtime.module_projection for insert
 with check(
   realtime.source_processable(tenant_id,source_id)
   and 'realtime:process'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true)))
   and ((lane='live' and replay_contexts='[]'::jsonb)
        or (lane='replay' and replay_contexts<>'[]'::jsonb))
 );
do $$ begin
 execute format('create policy migration_read on realtime.module_projection for select to %I using(true)',current_user);
end $$;
grant select,insert on realtime.module_projection to rocoto_rt;
grant usage,select on sequence realtime.module_projection_projection_id_seq to rocoto_rt;

-- Add module snapshots to the same tenant byte ledger used by source evidence.
create function realtime.module_usage(company text) returns table(projection_count bigint,projection_bytes bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select count(*),coalesce(sum(
   octet_length(input_manifest::text)+octet_length(snapshot::text)+
   octet_length(freshness::text)+octet_length(replay_contexts::text)+1024),0)::bigint
 from realtime.module_projection
 where tenant_id=company and realtime.company_visible(company)
$$;
revoke all on function realtime.module_usage(text) from public;
grant execute on function realtime.module_usage(text) to rocoto_rt;

alter function realtime.retained_usage(text) rename to retained_usage_subjects;
revoke all on function realtime.retained_usage_subjects(text) from rocoto_rt;
create function realtime.retained_usage(company text) returns table(
 event_count bigint,receipt_count bigint,pending_count bigint,quarantine_count bigint,retained_bytes bigint,
 source_count bigint,profile_count bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select b.event_count,b.receipt_count,b.pending_count,b.quarantine_count,b.retained_bytes+m.projection_bytes,
        b.source_count,b.profile_count
 from realtime.retained_usage_subjects(company) b cross join realtime.module_usage(company) m
$$;
revoke all on function realtime.retained_usage(text) from public;
grant execute on function realtime.retained_usage(text) to rocoto_rt;
