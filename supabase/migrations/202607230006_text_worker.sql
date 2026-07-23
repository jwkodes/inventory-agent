alter table public.source_events
  add column next_attempt_at timestamptz not null default now();

create index source_events_text_worker_idx
  on public.source_events (status, next_attempt_at, received_at, id)
  where provider = 'telegram' and event_type = 'message';

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
  v_event public.source_events%rowtype;
begin
  if not p_success and nullif(trim(p_error_message), '') is null then
    raise exception using errcode = '22023', message = 'Failed events require an error message';
  end if;

  select source_event.* into v_event
  from public.source_events as source_event
  where source_event.id = p_event_id
  for update;

  if not found or v_event.status <> 'processing' then
    return false;
  end if;

  if p_success then
    update public.source_events
    set status = 'processed',
        error_message = null,
        processed_at = now()
    where id = p_event_id;
    return true;
  end if;

  if v_event.processing_attempts >= 3 then
    update public.source_events
    set status = 'failed',
        error_message = left(trim(p_error_message), 1000),
        processed_at = now(),
        processing_started_at = null
    where id = p_event_id;
    return true;
  end if;

  update public.source_events
  set status = 'received',
      error_message = left(trim(p_error_message), 1000),
      processed_at = null,
      processing_started_at = null,
      next_attempt_at = now() + interval '30 seconds'
  where id = p_event_id;
  return true;
end;
$$;

create or replace function public.claim_next_telegram_text_event()
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
set search_path = public, pg_temp
as $$
declare
  v_event_id uuid;
begin
  select source_event.id into v_event_id
  from public.source_events as source_event
  where source_event.provider = 'telegram'
    and source_event.event_type = 'message'
    and (
      (
        source_event.status = 'received'
        and source_event.next_attempt_at <= now()
      )
      or (
        source_event.status = 'processing'
        and source_event.processing_started_at < now() - interval '15 minutes'
      )
    )
  order by source_event.received_at, source_event.id
  for update skip locked
  limit 1;

  if v_event_id is null then
    return;
  end if;

  return query
  select claimed.*
  from public.claim_telegram_text_event(v_event_id) as claimed;
end;
$$;

revoke all on function public.claim_next_telegram_text_event()
  from public, anon, authenticated;
grant execute on function public.claim_next_telegram_text_event() to service_role;

revoke all on function public.finish_source_event(uuid, boolean, text)
  from public, anon, authenticated;
grant execute on function public.finish_source_event(uuid, boolean, text) to service_role;

comment on function public.claim_next_telegram_text_event() is
  'Claims the oldest eligible Telegram text event with skip-locked worker concurrency.';
comment on function public.finish_source_event(uuid, boolean, text) is
  'Completes an event, retries failures after 30 seconds, or dead-letters the third failure.';
