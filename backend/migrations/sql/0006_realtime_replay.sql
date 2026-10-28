-- Replay is a persisted source-admission workflow, isolated from live delivery.
create table realtime.replay_run (
 tenant_id text not null references realtime.company_binding(tenant_id),
 run_id text not null check(length(run_id) between 1 and 256),
 principal_id text not null,
 request_id text not null default current_setting('rocoto.request_id',true),
 request_digest text not null check(request_digest ~ '^[0-9a-f]{64}$'),
 plan_digest text not null check(plan_digest ~ '^[0-9a-f]{64}$'),
 source_ids text[] not null check(cardinality(source_ids) between 1 and 16),
 plan jsonb not null check(octet_length(plan::text)<=20971520),
 cursor jsonb not null check(octet_length(cursor::text)<=4096),
 duration_s numeric not null check(duration_s>0 and duration_s<=3600),
 state text not null default 'preparing' check(state in ('preparing','running','paused','complete')),
 elapsed_s numeric not null default 0 check(elapsed_s>=0 and elapsed_s<=duration_s),
 clock_started_at timestamptz,
 revision integer not null default 0 check(revision>=0),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 primary key(tenant_id,run_id),
 check((state='running')=(clock_started_at is not null))
);

create function realtime.replay_visible(company text,actor text,sources text[]) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select realtime.company_visible(company)
 and (actor=current_setting('rocoto.principal',true)
      or 'realtime:configure'=any(realtime.actor_capabilities(company,current_setting('rocoto.principal',true))))
 and cardinality(sources) between 1 and 16
 and not exists(select 1 from unnest(sources) as ids(id)
   where not realtime.source_visible(company,ids.id)
   or not exists(select 1 from realtime.source s where s.tenant_id=company and s.source_id=ids.id and s.active))
$$;
revoke all on function realtime.replay_visible(text,text,text[]) from public;
grant execute on function realtime.replay_visible(text,text,text[]) to rocoto_rt;
alter table realtime.replay_run enable row level security;
alter table realtime.replay_run force row level security;
create policy replay_read on realtime.replay_run for select
 using(realtime.replay_visible(tenant_id,principal_id,source_ids));
create policy replay_insert on realtime.replay_run for insert with check(
 realtime.replay_visible(tenant_id,principal_id,source_ids)
 and 'realtime:configure'=any(realtime.actor_capabilities(tenant_id,principal_id))
 and 'realtime:ingest'=any(realtime.actor_capabilities(tenant_id,principal_id)));
create policy replay_update on realtime.replay_run for update using(
 realtime.replay_visible(tenant_id,principal_id,source_ids)
 and 'realtime:ingest'=any(realtime.actor_capabilities(tenant_id,principal_id)))
 with check(realtime.replay_visible(tenant_id,principal_id,source_ids));
grant select,insert on realtime.replay_run to rocoto_rt;
grant update(cursor,state,elapsed_s,clock_started_at,revision,updated_at) on realtime.replay_run to rocoto_rt;
do $$ begin
 execute format('create policy migration_read on realtime.replay_run for select to %I using(true)',current_user);
end $$;

create function realtime.protect_replay_identity() returns trigger
language plpgsql set search_path=pg_catalog,public,realtime as $$
begin
 if row(new.tenant_id,new.run_id,new.principal_id,new.request_id,new.request_digest,new.plan_digest,
        new.source_ids,new.plan,new.duration_s,new.created_at)
    is distinct from
    row(old.tenant_id,old.run_id,old.principal_id,old.request_id,old.request_digest,old.plan_digest,
        old.source_ids,old.plan,old.duration_s,old.created_at) then
   raise exception 'Replay identity and compiled plan are immutable' using errcode='23514';
 end if;
 return new;
end $$;
create trigger immutable_replay_identity before update on realtime.replay_run
 for each row execute function realtime.protect_replay_identity();

create function realtime.replay_usage(company text) returns table(plan_count bigint,plan_bytes bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select count(*),coalesce(sum(octet_length(plan::text)+octet_length(cursor::text)+1024),0)::bigint
 from realtime.replay_run where tenant_id=company and realtime.company_visible(company)
$$;
revoke all on function realtime.replay_usage(text) from public;
grant execute on function realtime.replay_usage(text) to rocoto_rt;

-- Preserve the foundation accounting, adding retained plan/cursor bytes for every principal.
alter function realtime.retained_usage(text) rename to retained_usage_foundation;
revoke all on function realtime.retained_usage_foundation(text) from rocoto_rt;
create function realtime.retained_usage(company text) returns table(
 event_count bigint,receipt_count bigint,pending_count bigint,quarantine_count bigint,retained_bytes bigint,
 source_count bigint,profile_count bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select b.event_count,b.receipt_count,b.pending_count,b.quarantine_count,b.retained_bytes+r.plan_bytes,
        b.source_count,b.profile_count
 from realtime.retained_usage_foundation(company) b cross join realtime.replay_usage(company) r
$$;
revoke all on function realtime.retained_usage(text) from public;
grant execute on function realtime.retained_usage(text) to rocoto_rt;
