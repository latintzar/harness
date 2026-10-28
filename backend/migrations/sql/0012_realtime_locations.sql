-- A retail location's trackers behind the company wall: identity and cameras, three-part tracker
-- revisions, the current output with a light ledger, retail records, the run schedule and the
-- newest runs. Every row stays: the runtime role holds select, insert and named column updates.

-- The run policy a location row holds is the document the SDK's SchedulePolicy accepts.
create function realtime.schedule_policy_valid(policy jsonb) returns boolean
language sql immutable set search_path=pg_catalog as $$
 select jsonb_typeof(policy)='object'
 and policy-array['automatic','min_interval_seconds','budget_seconds','daily_at','paused']='{}'::jsonb
 and coalesce(jsonb_typeof(policy->'automatic'),'boolean')='boolean'
 and case when not policy ? 'min_interval_seconds' then true
   when jsonb_typeof(policy->'min_interval_seconds')='number' and policy->>'min_interval_seconds' ~ '^[0-9]{1,5}$'
   then (policy->>'min_interval_seconds')::integer between 30 and 86400 else false end
 and case when not policy ? 'budget_seconds' then true
   when jsonb_typeof(policy->'budget_seconds')='number' and policy->>'budget_seconds' ~ '^[0-9]{1,3}$'
   then (policy->>'budget_seconds')::integer between 5 and 180 else false end
 and case when coalesce(jsonb_typeof(policy->'daily_at'),'null')='null' then true
   else jsonb_typeof(policy->'daily_at')='string' and policy->>'daily_at' ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$' end
 and case when not policy ? 'paused' then true
   when jsonb_typeof(policy->'paused')<>'array' then false
   else jsonb_array_length(policy->'paused')<=100
     and not exists(select 1 from jsonb_array_elements(policy->'paused') e
       where jsonb_typeof(e)<>'string' or e#>>'{}' !~ '^[a-z][a-z0-9_-]{0,79}$')
     and (select count(distinct e)=count(*) from jsonb_array_elements(policy->'paused') e) end
$$;
revoke all on function realtime.schedule_policy_valid(jsonb) from public;
grant execute on function realtime.schedule_policy_valid(jsonb) to rocoto_rt;

create table realtime.location (
 tenant_id text not null references realtime.company_binding(tenant_id),
 location_id text not null check(length(location_id) between 1 and 256),
 collection text not null check(length(collection) between 1 and 64),
 business_type text not null check(business_type in ('retail')),
 -- What a commit expects: it moves with every commit of a run and every tracker change.
 run_seq bigint not null default 0 check(run_seq>=0),
 -- The page's revision: it moves when a commit or a tracker change altered what the page shows.
 page_seq bigint not null default 0 check(page_seq>=0),
 -- The highest realtime.event.sequence a committed run has read.
 through_sequence bigint not null default 0 check(through_sequence>=0),
 -- The run schedule: a typed policy with its own revision, a manual request, back-off and the lease.
 policy jsonb not null default '{}'::jsonb
   constraint location_policy_bytes check(octet_length(policy::text)<=4096)
   constraint location_policy_shape check(realtime.schedule_policy_valid(policy)),
 policy_revision integer not null default 1 check(policy_revision>=1),
 processed_stamp text check(length(processed_stamp) between 1 and 128),
 processed_day date,
 attempt_stamp text check(length(attempt_stamp) between 1 and 128),
 failures integer not null default 0 check(failures between 0 and 1000000),
 next_after timestamptz,
 manual_after timestamptz,
 requested_at timestamptz,
 requested_by text check(length(requested_by) between 1 and 256),
 requested_only jsonb constraint location_requested_only_bytes
   check(jsonb_typeof(requested_only)='array' and octet_length(requested_only::text)<=4096),
 lease_token text check(length(lease_token) between 16 and 128),
 lease_until timestamptz,
 lease_run_id text check(length(lease_run_id) between 1 and 256),
 last_run_id text check(length(last_run_id) between 1 and 256),
 last_status text check(last_status in ('running','completed','failed','budget_exceeded','interrupted')),
 run_count bigint not null default 0 check(run_count>=0),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,location_id),
 check((lease_token is null)=(lease_until is null) and (lease_token is null)=(lease_run_id is null)),
 check((requested_at is null)=(requested_by is null) and (requested_only is null or requested_at is not null))
);
create table realtime.location_source (
 tenant_id text not null, location_id text not null, source_id text not null,
 camera_id text not null check(length(camera_id) between 1 and 256),
 body jsonb not null default '{}'::jsonb constraint location_source_body_bytes
   check(jsonb_typeof(body)='object' and octet_length(body::text)<=4096),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,location_id,source_id),
 unique(tenant_id,source_id),
 foreign key(tenant_id,location_id) references realtime.location(tenant_id,location_id),
 foreign key(tenant_id,source_id) references realtime.source(tenant_id,source_id)
);

-- A location is read with its cameras: every bound camera is visible to this principal. A location
-- that holds no camera yet is read by the principals who configure the company.
create function realtime.location_visible(company text, location text) returns boolean
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select realtime.company_visible(company)
 and ('realtime:configure'=any(realtime.actor_capabilities(company,current_setting('rocoto.principal',true)))
      or exists(select 1 from realtime.location_source s where s.tenant_id=company and s.location_id=location))
 and not exists(select 1 from realtime.location_source s where s.tenant_id=company and s.location_id=location
   and not realtime.source_visible(company,s.source_id))
$$;
revoke all on function realtime.location_visible(text,text) from public;
grant execute on function realtime.location_visible(text,text) to rocoto_rt;

-- Trackers of a location: identity with its head, and append-only revisions of three parts.
-- A Core Tracker (core = true) keeps its key, holds a definition only, has no chat and stays active.
-- Which definition a Core Tracker holds is configuration's to install, as the recipe of its business
-- type names it. The runner holds the other half: it runs a Core Tracker's processor.script in its
-- own process only when that script equals the one its maintained recipe names.
create table realtime.location_tracker (
 tenant_id text not null,
 tracker_id text not null check(length(tracker_id) between 1 and 256),
 location_id text not null,
 key text not null check(key ~ '^[a-z][a-z0-9_-]{0,79}$'),
 core boolean not null,
 kind text not null check(kind in ('core_tracker','company_tracker','tracking_agent')),
 chat_id text check(length(chat_id) between 1 and 256),
 definition_revision integer not null,
 script_revision integer,
 presentation_revision integer,
 state text not null default 'active' check(state in ('active','paused','retired')),
 head_revision integer not null default 1 check(head_revision>=1),
 definition_part text not null generated always as ('definition') stored,
 script_part text not null generated always as ('script') stored,
 presentation_part text not null generated always as ('presentation') stored,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,tracker_id),
 unique(tenant_id,location_id,key),
 unique(tenant_id,tracker_id,location_id),
 unique(tenant_id,tracker_id,location_id,core),
 foreign key(tenant_id,location_id) references realtime.location(tenant_id,location_id),
 constraint location_tracker_core_kind check(core=(kind='core_tracker')),
 constraint location_tracker_core_locked check(not core or (chat_id is null and script_revision is null
   and presentation_revision is null and state='active'))
);
create table realtime.location_tracker_revision (
 tenant_id text not null, tracker_id text not null, location_id text not null, core boolean not null,
 part text not null check(part in ('definition','script','presentation')),
 revision integer not null check(revision between 1 and 100000),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 body jsonb not null constraint location_tracker_revision_body_bytes
   check(jsonb_typeof(body)='object' and octet_length(body::text)<=262144),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 conversation_id text not null check(length(conversation_id) between 1 and 256),
 primary key(tenant_id,tracker_id,part,revision),
 foreign key(tenant_id,tracker_id,location_id,core)
   references realtime.location_tracker(tenant_id,tracker_id,location_id,core),
 constraint location_tracker_revision_core_locked check(not core or part='definition'),
 -- A definition document states the Core status its tracker has.
 constraint location_tracker_revision_core_flag
   check(part<>'definition' or core=coalesce(body->'core'='true'::jsonb,false))
);
-- A head names saved revisions of its own tracker; the pair is written in one transaction.
alter table realtime.location_tracker
 add constraint location_tracker_definition_head
   foreign key(tenant_id,tracker_id,definition_part,definition_revision)
   references realtime.location_tracker_revision(tenant_id,tracker_id,part,revision) deferrable initially deferred,
 add constraint location_tracker_script_head
   foreign key(tenant_id,tracker_id,script_part,script_revision)
   references realtime.location_tracker_revision(tenant_id,tracker_id,part,revision) deferrable initially deferred,
 add constraint location_tracker_presentation_head
   foreign key(tenant_id,tracker_id,presentation_part,presentation_revision)
   references realtime.location_tracker_revision(tenant_id,tracker_id,part,revision) deferrable initially deferred;

create function realtime.protect_location_tracker() returns trigger
language plpgsql set search_path=pg_catalog,public,realtime as $$
begin
 if row(new.tenant_id,new.tracker_id,new.location_id,new.key,new.core,new.kind,new.chat_id,new.created_at)
    is distinct from
    row(old.tenant_id,old.tracker_id,old.location_id,old.key,old.core,old.kind,old.chat_id,old.created_at) then
   raise exception 'Tracker identity, key, chat and Core status are immutable' using errcode='23514';
 end if;
 if new.head_revision<>old.head_revision+1 then
   raise exception 'A tracker head moves one revision at a time' using errcode='23514';
 end if;
 return new;
end $$;
create trigger immutable_location_tracker before update on realtime.location_tracker
 for each row execute function realtime.protect_location_tracker();

-- Outputs: the current one in place, and a light ledger of every published revision. Both are
-- identified within their location, so a writer meets rows of the locations it reaches.
create table realtime.location_tracker_output (
 tenant_id text not null, tracker_id text not null, location_id text not null,
 revision integer not null check(revision>=1),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 period jsonb not null constraint location_tracker_output_period_bytes check(octet_length(period::text)<=1024),
 tile jsonb not null constraint location_tracker_output_tile_bytes
   check(jsonb_typeof(tile)='object' and octet_length(tile::text)<=262144),
 canvas jsonb not null constraint location_tracker_output_canvas_bytes
   check(jsonb_typeof(canvas)='object' and octet_length(canvas::text)<=1048576),
 periods jsonb not null default '{}'::jsonb constraint location_tracker_output_periods_bytes
   check(jsonb_typeof(periods)='object' and octet_length(periods::text)<=1048576),
 input jsonb not null constraint location_tracker_output_input_bytes
   check(jsonb_typeof(input)='object' and octet_length(input::text)<=8192),
 updated_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,location_id,tracker_id),
 foreign key(tenant_id,tracker_id,location_id) references realtime.location_tracker(tenant_id,tracker_id,location_id)
);
create table realtime.location_tracker_output_revision (
 tenant_id text not null, tracker_id text not null, location_id text not null,
 revision integer not null check(revision>=1),
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 period jsonb not null constraint location_tracker_output_revision_period_bytes
   check(octet_length(period::text)<=1024),
 summary jsonb not null constraint location_tracker_output_revision_summary_bytes
   check(jsonb_typeof(summary)='object' and octet_length(summary::text)<=4096),
 input jsonb not null constraint location_tracker_output_revision_input_bytes
   check(jsonb_typeof(input)='object' and octet_length(input::text)<=8192),
 created_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,location_id,tracker_id,revision),
 foreign key(tenant_id,tracker_id,location_id) references realtime.location_tracker(tenant_id,tracker_id,location_id)
);

-- Retail records of a location: derived (rebuilt by a run), ingested (evidence, till) or
-- configured (catalog, zones). A row keeps its basis; a derived row that leaves a rebuild turns inactive.
create sequence realtime.location_record_mark;
grant usage on sequence realtime.location_record_mark to rocoto_rt;
create table realtime.location_record (
 tenant_id text not null, location_id text not null,
 kind text not null check(kind in ('observed_item','basket','sale','sale_line','shelf_state','queue_interval',
   'page','recording','event_evidence','item_grounding','basket_evidence','tracked_visit','product','zone')),
 record_id text not null check(length(record_id) between 1 and 256),
 basis text not null check(basis in ('derived','ingested','configured')),
 source_id text check(length(source_id) between 1 and 256),
 occurred_at timestamptz,
 active boolean not null default true,
 digest text not null check(digest ~ '^[a-f0-9]{64}$'),
 -- The page document of a location is one record of up to 1 MiB; every other record is small.
 body jsonb not null constraint location_record_body_bytes check(jsonb_typeof(body)='object'
   and octet_length(body::text)<=case when kind='page' then 1048576 else 65536 end),
 -- Drawn from one sequence at every write of the row.
 mark bigint not null default nextval('realtime.location_record_mark'),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 -- A record is identified within its location, so a writer meets rows of the locations it reaches.
 primary key(tenant_id,location_id,kind,record_id),
 foreign key(tenant_id,location_id) references realtime.location(tenant_id,location_id),
 -- Each kind takes the bases its writers hold: a run derives, evidence and the till are ingested,
 -- the catalog and the zones are configured.
 constraint location_record_kind_basis check(case basis
   when 'derived' then kind in ('observed_item','basket','shelf_state','queue_interval','page','sale','sale_line','product')
   when 'ingested' then kind in ('sale','sale_line','product','recording','event_evidence','item_grounding',
     'basket_evidence','tracked_visit')
   when 'configured' then kind in ('product','zone')
   else false end)
);
create index realtime_location_record_read on realtime.location_record(tenant_id,location_id,kind,record_id) where active;
create index realtime_location_record_period on realtime.location_record(tenant_id,location_id,kind,occurred_at) where active;
-- Every write of a record draws a higher mark, so the sum of marks over a location's input rows
-- rises with each committed write, in whatever order the writers began and committed.
create function realtime.mark_location_record() returns trigger
language plpgsql set search_path=pg_catalog,public,realtime as $$
begin
 new.mark := nextval('realtime.location_record_mark');
 return new;
end $$;
create trigger mark_location_record before insert or update on realtime.location_record
 for each row execute function realtime.mark_location_record();

-- The newest 200 runs of a location. Run number N lives in slot (N-1) mod 200, so run N takes the
-- place of run N-200 with an update in place: the table holds 200 rows per location at most.
create table realtime.location_run (
 tenant_id text not null, location_id text not null,
 slot integer not null check(slot between 0 and 199),
 ordinal bigint not null check(ordinal>=1),
 run_id text not null check(length(run_id) between 1 and 256),
 trigger text not null check(trigger in ('events','day','manual','host')),
 status text not null check(status in ('running','completed','failed','budget_exceeded','interrupted')),
 started_at timestamptz not null default now(),
 finished_at timestamptz,
 input_stamp text not null check(length(input_stamp) between 1 and 128),
 day date,
 wall_ms integer check(wall_ms between 0 and 86400000),
 period jsonb constraint location_run_period_bytes check(octet_length(period::text)<=1024),
 derived jsonb constraint location_run_derived_bytes check(octet_length(derived::text)<=4096),
 outputs jsonb not null default '[]'::jsonb constraint location_run_outputs_bytes
   check(jsonb_typeof(outputs)='array' and octet_length(outputs::text)<=32768),
 errors jsonb not null default '[]'::jsonb constraint location_run_errors_bytes
   check(jsonb_typeof(errors)='array' and octet_length(errors::text)<=16384),
 skipped jsonb not null default '[]'::jsonb constraint location_run_skipped_bytes
   check(jsonb_typeof(skipped)='array' and octet_length(skipped::text)<=8192),
 error text check(length(error)<=300),
 published boolean not null default false,
 requested_by text check(length(requested_by) between 1 and 256),
 requested_only jsonb constraint location_run_requested_only_bytes
   check(jsonb_typeof(requested_only)='array' and octet_length(requested_only::text)<=4096),
 -- A run commits once: the digest of what it committed and the answer it got, for an exact retry.
 commit_digest text check(commit_digest ~ '^[a-f0-9]{64}$'),
 committed jsonb constraint location_run_committed_bytes
   check(jsonb_typeof(committed)='object' and octet_length(committed::text)<=262144),
 principal_id text not null default current_setting('rocoto.principal',true),
 request_id text not null default current_setting('rocoto.request_id',true),
 primary key(tenant_id,location_id,slot),
 -- A run id is 128 random bits the SDK draws; finish and commit find a run by it.
 unique(tenant_id,run_id),
 unique(tenant_id,location_id,ordinal),
 foreign key(tenant_id,location_id) references realtime.location(tenant_id,location_id),
 constraint location_run_ring check(slot=((ordinal-1)%200)::integer),
 constraint location_run_closed check((status='running')=(finished_at is null)),
 constraint location_run_committed check((commit_digest is null)=(committed is null))
);

-- The locations of the current scope that this principal reads (capability null) or writes with the
-- named capability. Processing and ingestion also follow the grant on each camera of the location,
-- as they do for the camera's own events and outbox. A policy asks once per statement and holds
-- the answer for every row it checks.
create function realtime.location_reach(capability text) returns table(tenant_id text,location_id text)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select l.tenant_id,l.location_id from realtime.location l
 join realtime.company_binding b on b.tenant_id=l.tenant_id
 where b.scope=nullif(current_setting('rocoto.scope',true),'')::ltree
 and realtime.location_visible(l.tenant_id,l.location_id)
 and (capability is null
   or capability=any(realtime.actor_capabilities(l.tenant_id,current_setting('rocoto.principal',true))))
 and (capability is distinct from 'realtime:process' or not exists(
   select 1 from realtime.location_source s where s.tenant_id=l.tenant_id and s.location_id=l.location_id
   and not realtime.source_processable(s.tenant_id,s.source_id)))
 and (capability is distinct from 'realtime:ingest' or not exists(
   select 1 from realtime.location_source s where s.tenant_id=l.tenant_id and s.location_id=l.location_id
   and not realtime.source_writable(s.tenant_id,s.source_id)))
$$;
revoke all on function realtime.location_reach(text) from public;
grant execute on function realtime.location_reach(text) to rocoto_rt;

-- Who writes: configuration registers locations, cameras and trackers; processing writes outputs
-- and runs; a record is written with the capability its basis names. The location row carries the
-- schedule (configuration) beside the run cursor, lease and back-off (processing); its trigger
-- below holds each part to its own capability.
do $$ declare tab text; pair text[]; reach text; begin
 reach := '(tenant_id,location_id) in (select r.tenant_id,r.location_id from realtime.location_reach(%L) r)';
 foreach tab in array array['location','location_source','location_tracker','location_tracker_revision',
   'location_tracker_output','location_tracker_output_revision','location_record','location_run'] loop
  execute format('alter table realtime.%I enable row level security',tab);
  execute format('alter table realtime.%I force row level security',tab);
  execute format('create policy migration_read on realtime.%I for select to %I using(true)',tab,current_user);
  execute format('create policy scoped_read on realtime.%I for select using('||format(reach,null)||')',tab);
  execute format('grant select,insert on realtime.%I to rocoto_rt',tab);
 end loop;
 foreach pair slice 1 in array array[
   ['location_tracker','realtime:configure'],['location_tracker_revision','realtime:configure'],
   ['location_tracker_output','realtime:process'],['location_tracker_output_revision','realtime:process'],
   ['location_run','realtime:process']] loop
  execute format('create policy scoped_insert on realtime.%I for insert with check('||format(reach,pair[2])||')',pair[1]);
 end loop;
 foreach pair slice 1 in array array[
   ['location_tracker','realtime:configure'],['location_tracker_output','realtime:process'],
   ['location_run','realtime:process']] loop
  execute format('create policy scoped_update on realtime.%I for update using('||format(reach,pair[2])
    ||') with check('||format(reach,pair[2])||')',pair[1]);
 end loop;
 execute 'create policy scoped_insert on realtime.location_source for insert with check('
   ||format(reach,'realtime:configure')||' and realtime.source_visible(tenant_id,source_id))';
 execute 'create policy scoped_update on realtime.location for update using('
   ||format(reach,'realtime:configure')||' or '||format(reach,'realtime:process')||') with check('
   ||format(reach,'realtime:configure')||' or '||format(reach,'realtime:process')||')';
 reach := '((basis=''derived'' and '||format(reach,'realtime:process')||') or (basis=''ingested'' and '
   ||format(reach,'realtime:ingest')||') or (basis=''configured'' and '||format(reach,'realtime:configure')||'))';
 execute 'create policy scoped_insert on realtime.location_record for insert with check('||reach||')';
 execute 'create policy scoped_update on realtime.location_record for update using('||reach
   ||') with check('||reach||')';
end $$;
create policy scoped_insert on realtime.location for insert with check(
 realtime.company_visible(tenant_id)
 and 'realtime:configure'=any(realtime.actor_capabilities(tenant_id,current_setting('rocoto.principal',true))));

-- One location row, two writers. Where row security binds the caller, the run policy changes with
-- configuration, and the cursor, the lease, the back-off, the request and the run pointers change
-- with processing. Both revisions move with either, and forward for every writer.
create function realtime.protect_location() returns trigger
language plpgsql set search_path=pg_catalog,public,realtime as $$
begin
 if new.run_seq<old.run_seq or new.page_seq<old.page_seq or new.policy_revision<old.policy_revision then
   raise exception 'A location revision moves forward' using errcode='23514';
 end if;
 if not row_security_active('realtime.location'::regclass) then
   return new;
 end if;
 if row(new.policy,new.policy_revision) is distinct from row(old.policy,old.policy_revision)
    and not exists(select 1 from realtime.location_reach('realtime:configure') r
      where r.tenant_id=new.tenant_id and r.location_id=new.location_id) then
   raise exception 'The run schedule of a location changes with realtime:configure' using errcode='42501';
 end if;
 if row(new.through_sequence,new.processed_stamp,new.processed_day,new.attempt_stamp,new.failures,
        new.next_after,new.manual_after,new.requested_at,new.requested_by,new.requested_only,
        new.lease_token,new.lease_until,new.lease_run_id,new.last_run_id,new.last_status,new.run_count)
    is distinct from
    row(old.through_sequence,old.processed_stamp,old.processed_day,old.attempt_stamp,old.failures,
        old.next_after,old.manual_after,old.requested_at,old.requested_by,old.requested_only,
        old.lease_token,old.lease_until,old.lease_run_id,old.last_run_id,old.last_status,old.run_count)
    and not exists(select 1 from realtime.location_reach('realtime:process') r
      where r.tenant_id=new.tenant_id and r.location_id=new.location_id) then
   raise exception 'The run cursor, lease, request and back-off of a location change with realtime:process'
     using errcode='42501';
 end if;
 return new;
end $$;
create trigger protect_location before update on realtime.location
 for each row execute function realtime.protect_location();

-- What a run of a location reads, as counts and high-water marks: the live events of its cameras
-- with their subject bindings, the ingested and configured records, the cameras and the tracker
-- heads. The answer is null outside the principal's reach.
create function realtime.location_inputs(company text, location text) returns jsonb
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 with cameras as (select source_id,camera_id from realtime.location_source
   where tenant_id=company and location_id=location)
 select jsonb_build_object(
  'events',(select jsonb_build_array(count(*),coalesce(max(e.sequence),0)) from realtime.event e
    where e.tenant_id=company and e.replay_context is null and e.source_id in (select source_id from cameras)),
  'bindings',(select count(*) from realtime.event_subject_set b
    where b.tenant_id=company and b.source_id in (select source_id from cameras)),
  'subjects',(select count(*) from realtime.subject_revision r
    where r.tenant_id=company and r.source_id in (select source_id from cameras)),
  'records',(select jsonb_build_array(count(*),coalesce(sum(mark),0)) from realtime.location_record
    where tenant_id=company and location_id=location and basis<>'derived'),
  'cameras',(select coalesce(jsonb_agg(jsonb_build_array(source_id,camera_id) order by source_id),'[]'::jsonb)
    from cameras),
  'trackers',(select coalesce(jsonb_agg(jsonb_build_array(tracker_id,head_revision) order by tracker_id),'[]'::jsonb)
    from realtime.location_tracker where tenant_id=company and location_id=location))
 where realtime.location_visible(company,location)
 and exists(select 1 from realtime.location l where l.tenant_id=company and l.location_id=location)
$$;
revoke all on function realtime.location_inputs(text,text) from public;
grant execute on function realtime.location_inputs(text,text) to rocoto_rt;

-- Column updates only. Identity, the three revision tables' rows, camera bindings and the output
-- ledger are written once.
grant update(run_seq,page_seq,through_sequence,policy,policy_revision,processed_stamp,processed_day,
 attempt_stamp,failures,next_after,manual_after,requested_at,requested_by,requested_only,lease_token,
 lease_until,lease_run_id,last_run_id,last_status,run_count,updated_at) on realtime.location to rocoto_rt;
grant update(definition_revision,script_revision,presentation_revision,state,head_revision,updated_at)
 on realtime.location_tracker to rocoto_rt;
grant update(revision,digest,period,tile,canvas,periods,input,updated_at,principal_id,request_id)
 on realtime.location_tracker_output to rocoto_rt;
grant update(source_id,occurred_at,active,digest,body,updated_at,principal_id,request_id)
 on realtime.location_record to rocoto_rt;
grant update(ordinal,run_id,trigger,status,started_at,finished_at,input_stamp,day,wall_ms,period,derived,
 outputs,errors,skipped,error,published,requested_by,requested_only,commit_digest,committed,principal_id,
 request_id) on realtime.location_run to rocoto_rt;

-- Location rows join the company byte ledger used by source evidence.
create function realtime.location_usage(company text) returns table(
 location_count bigint,tracker_count bigint,record_count bigint,retained_bytes bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 with l as(select count(*) n,coalesce(sum(octet_length(policy::text)+coalesce(octet_length(requested_only::text),0)+2048),0)::bigint b
   from realtime.location where tenant_id=company),
 s as(select coalesce(sum(octet_length(body::text)+1024),0)::bigint b from realtime.location_source where tenant_id=company),
 t as(select count(*) n,count(*)*2048 b from realtime.location_tracker where tenant_id=company),
 v as(select coalesce(sum(octet_length(body::text)+1024),0)::bigint b from realtime.location_tracker_revision where tenant_id=company),
 o as(select coalesce(sum(octet_length(period::text)+octet_length(tile::text)+octet_length(canvas::text)
   +octet_length(periods::text)+octet_length(input::text)+1024),0)::bigint b
   from realtime.location_tracker_output where tenant_id=company),
 h as(select coalesce(sum(octet_length(period::text)+octet_length(summary::text)+octet_length(input::text)+1024),0)::bigint b
   from realtime.location_tracker_output_revision where tenant_id=company),
 r as(select count(*) n,coalesce(sum(octet_length(body::text)+1024),0)::bigint b from realtime.location_record where tenant_id=company),
 u as(select coalesce(sum(coalesce(octet_length(period::text),0)+coalesce(octet_length(derived::text),0)
   +octet_length(outputs::text)+octet_length(errors::text)+octet_length(skipped::text)
   +coalesce(octet_length(requested_only::text),0)+coalesce(octet_length(committed::text),0)+1024),0)::bigint b
   from realtime.location_run where tenant_id=company)
 select l.n,t.n,r.n,l.b+s.b+t.b+v.b+o.b+h.b+r.b+u.b from l,s,t,v,o,h,r,u where realtime.company_visible(company)
$$;
revoke all on function realtime.location_usage(text) from public;
grant execute on function realtime.location_usage(text) to rocoto_rt;
alter function realtime.retained_usage(text) rename to retained_usage_configuration;
revoke all on function realtime.retained_usage_configuration(text) from rocoto_rt;
create function realtime.retained_usage(company text) returns table(
 event_count bigint,receipt_count bigint,pending_count bigint,quarantine_count bigint,retained_bytes bigint,
 source_count bigint,profile_count bigint)
language sql stable security definer set search_path=pg_catalog,public,realtime as $$
 select b.event_count,b.receipt_count,b.pending_count,b.quarantine_count,b.retained_bytes+l.retained_bytes,
 b.source_count,b.profile_count from realtime.retained_usage_configuration(company) b
 cross join realtime.location_usage(company) l
$$;
revoke all on function realtime.retained_usage(text) from public;
grant execute on function realtime.retained_usage(text) to rocoto_rt;
