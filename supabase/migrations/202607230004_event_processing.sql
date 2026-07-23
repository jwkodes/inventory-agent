create type public.processing_outcome_type as enum (
  'proposal_ready',
  'clarification_required',
  'unsupported_command'
);

create type public.outbox_status as enum ('pending', 'sent', 'failed');

alter table public.source_events
  add column processing_started_at timestamptz,
  add column processing_attempts integer not null default 0,
  add constraint source_events_processing_attempts_nonnegative
    check (processing_attempts >= 0);

create table public.processing_outbox (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  source_event_id uuid not null,
  outcome_type public.processing_outcome_type not null,
  aggregate_id uuid,
  chat_id bigint not null,
  payload jsonb not null default '{}'::jsonb,
  status public.outbox_status not null default 'pending',
  attempts integer not null default 0,
  error_message text,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  foreign key (organization_id, source_event_id)
    references public.source_events (organization_id, id) on delete cascade,
  unique (organization_id, id),
  unique (source_event_id),
  check (attempts >= 0),
  check (
    (outcome_type = 'proposal_ready' and aggregate_id is not null)
    or (outcome_type <> 'proposal_ready' and aggregate_id is null)
  )
);

create index processing_outbox_pending_idx
  on public.processing_outbox (created_at, id)
  where status = 'pending';

alter table public.processing_outbox enable row level security;

grant select, insert, update, delete on public.processing_outbox to service_role;

create or replace function public.claim_telegram_text_event(p_event_id uuid)
returns table (
  event_id uuid,
  organization_id uuid,
  organization_user_id uuid,
  location_id uuid,
  external_event_id text,
  chat_id bigint,
  telegram_user_id bigint,
  message_text text
)
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_event public.source_events%rowtype;
  v_member_id uuid;
  v_location_id uuid;
  v_message_text text;
  v_chat_id_text text;
  v_user_id_text text;
begin
  select source_event.* into v_event
  from public.source_events as source_event
  where source_event.id = p_event_id
  for update;

  if not found
     or (
       v_event.status <> 'received'
       and not (
         v_event.status = 'processing'
         and v_event.processing_started_at < now() - interval '15 minutes'
       )
     ) then
    return;
  end if;

  if v_event.provider <> 'telegram' or v_event.event_type <> 'message' then
    update public.source_events
    set status = 'failed',
        error_message = 'Event is not a Telegram message',
        processed_at = now()
    where id = v_event.id;
    return;
  end if;

  v_message_text := nullif(trim(v_event.payload #>> '{message,text}'), '');
  v_chat_id_text := v_event.payload #>> '{message,chat,id}';
  v_user_id_text := v_event.payload #>> '{message,from,id}';

  if v_message_text is null
     or v_chat_id_text is null
     or v_chat_id_text !~ '^-?[0-9]+$'
     or v_user_id_text is null
     or v_user_id_text !~ '^[0-9]+$' then
    update public.source_events
    set status = 'failed',
        error_message = 'Telegram message is missing valid text, chat, or sender data',
        processed_at = now()
    where id = v_event.id;
    return;
  end if;

  select member.id into v_member_id
  from public.organization_users as member
  where member.organization_id = v_event.organization_id
    and member.telegram_user_id = v_user_id_text::bigint
    and member.active;

  if v_member_id is null then
    update public.source_events
    set status = 'failed',
        error_message = 'Telegram sender is not an active organization member',
        processed_at = now()
    where id = v_event.id;
    return;
  end if;

  select location.id into v_location_id
  from public.locations as location
  join public.organizations as organization
    on organization.id = location.organization_id
  where location.organization_id = v_event.organization_id
    and location.active
  order by
    (location.id::text = organization.settings ->> 'default_location_id') desc,
    location.code,
    location.id
  limit 1;

  if v_location_id is null then
    update public.source_events
    set status = 'failed',
        error_message = 'Organization has no active inventory location',
        processed_at = now()
    where id = v_event.id;
    return;
  end if;

  update public.source_events
  set status = 'processing',
      error_message = null,
      processed_at = null,
      processing_started_at = now(),
      processing_attempts = processing_attempts + 1
  where id = v_event.id;

  return query
  select
    v_event.id,
    v_event.organization_id,
    v_member_id,
    v_location_id,
    v_event.external_event_id,
    v_chat_id_text::bigint,
    v_user_id_text::bigint,
    v_message_text;
end;
$$;

create or replace function public.finish_source_event(
  p_event_id uuid,
  p_success boolean,
  p_error_message text default null
)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_updated boolean;
begin
  if not p_success and nullif(trim(p_error_message), '') is null then
    raise exception using errcode = '22023', message = 'Failed events require an error message';
  end if;

  update public.source_events
  set status = case
        when p_success then 'processed'::public.source_event_status
        else 'failed'::public.source_event_status
      end,
      error_message = case when p_success then null else left(trim(p_error_message), 1000) end,
      processed_at = now()
  where id = p_event_id
    and status = 'processing';

  v_updated := found;
  return v_updated;
end;
$$;

create or replace function public.enqueue_processing_outcome(
  p_organization_id uuid,
  p_source_event_id uuid,
  p_outcome_type public.processing_outcome_type,
  p_aggregate_id uuid,
  p_chat_id bigint,
  p_payload jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_outbox_id uuid;
begin
  if not exists (
    select 1 from public.source_events as source_event
    where source_event.organization_id = p_organization_id
      and source_event.id = p_source_event_id
  ) then
    raise exception using errcode = '22023', message = 'Source event is not in the organization';
  end if;

  if p_outcome_type = 'proposal_ready' and not exists (
    select 1 from public.transaction_proposals as proposal
    where proposal.organization_id = p_organization_id
      and proposal.id = p_aggregate_id
  ) then
    raise exception using errcode = '22023', message = 'Proposal is not in the organization';
  end if;

  insert into public.processing_outbox (
    organization_id,
    source_event_id,
    outcome_type,
    aggregate_id,
    chat_id,
    payload
  )
  values (
    p_organization_id,
    p_source_event_id,
    p_outcome_type,
    p_aggregate_id,
    p_chat_id,
    coalesce(p_payload, '{}'::jsonb)
  )
  on conflict (source_event_id) do nothing
  returning id into v_outbox_id;

  if v_outbox_id is null then
    select outbox.id into v_outbox_id
    from public.processing_outbox as outbox
    where outbox.source_event_id = p_source_event_id;
  end if;

  return v_outbox_id;
end;
$$;

revoke all on function public.claim_telegram_text_event(uuid)
  from public, anon, authenticated;
revoke all on function public.finish_source_event(uuid, boolean, text)
  from public, anon, authenticated;
revoke all on function public.enqueue_processing_outcome(
  uuid, uuid, public.processing_outcome_type, uuid, bigint, jsonb
) from public, anon, authenticated;

grant execute on function public.claim_telegram_text_event(uuid) to service_role;
grant execute on function public.finish_source_event(uuid, boolean, text) to service_role;
grant execute on function public.enqueue_processing_outcome(
  uuid, uuid, public.processing_outcome_type, uuid, bigint, jsonb
) to service_role;

comment on table public.processing_outbox is
  'Durable, idempotent handoff from source processing to outbound message delivery.';
comment on function public.claim_telegram_text_event(uuid) is
  'Atomically claims one received Telegram text event and resolves its member and location.';
comment on function public.finish_source_event(uuid, boolean, text) is
  'Transitions a claimed source event to processed or failed.';
comment on function public.enqueue_processing_outcome(
  uuid, uuid, public.processing_outcome_type, uuid, bigint, jsonb
) is 'Idempotently records an outbound outcome for later Telegram delivery.';
