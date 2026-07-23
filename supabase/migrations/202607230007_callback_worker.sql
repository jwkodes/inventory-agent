create or replace function public.claim_telegram_callback_event(p_event_id uuid)
returns table (
  event_id uuid,
  organization_id uuid,
  organization_user_id uuid,
  external_event_id text,
  callback_query_id text,
  callback_data text,
  chat_id bigint,
  telegram_message_id bigint,
  telegram_user_id bigint
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_event public.source_events%rowtype;
  v_member_id uuid;
  v_callback_query_id text;
  v_callback_data text;
  v_chat_id_text text;
  v_message_id_text text;
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

  if v_event.provider <> 'telegram' or v_event.event_type <> 'callback_query' then
    update public.source_events
    set status = 'failed',
        error_message = 'Event is not a Telegram callback query',
        processed_at = now()
    where id = v_event.id;
    return;
  end if;

  v_callback_query_id := nullif(trim(v_event.payload #>> '{callback_query,id}'), '');
  v_callback_data := nullif(v_event.payload #>> '{callback_query,data}', '');
  v_chat_id_text := v_event.payload #>> '{callback_query,message,chat,id}';
  v_message_id_text := v_event.payload #>> '{callback_query,message,message_id}';
  v_user_id_text := v_event.payload #>> '{callback_query,from,id}';

  if v_callback_query_id is null
     or v_callback_data is null
     or v_chat_id_text is null
     or v_chat_id_text !~ '^-?[0-9]+$'
     or v_message_id_text is null
     or v_message_id_text !~ '^[0-9]+$'
     or v_user_id_text is null
     or v_user_id_text !~ '^[0-9]+$' then
    update public.source_events
    set status = 'failed',
        error_message = 'Telegram callback is missing valid query, data, message, chat, or sender data',
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
        error_message = 'Telegram callback sender is not an active organization member',
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
    v_event.external_event_id,
    v_callback_query_id,
    v_callback_data,
    v_chat_id_text::bigint,
    v_message_id_text::bigint,
    v_user_id_text::bigint;
end;
$$;

create or replace function public.claim_next_telegram_callback_event()
returns table (
  event_id uuid,
  organization_id uuid,
  organization_user_id uuid,
  external_event_id text,
  callback_query_id text,
  callback_data text,
  chat_id bigint,
  telegram_message_id bigint,
  telegram_user_id bigint
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
    and source_event.event_type = 'callback_query'
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
  from public.claim_telegram_callback_event(v_event_id) as claimed;
end;
$$;

revoke all on function public.claim_telegram_callback_event(uuid)
  from public, anon, authenticated;
revoke all on function public.claim_next_telegram_callback_event()
  from public, anon, authenticated;
grant execute on function public.claim_telegram_callback_event(uuid) to service_role;
grant execute on function public.claim_next_telegram_callback_event() to service_role;

comment on function public.claim_telegram_callback_event(uuid) is
  'Claims one Telegram callback and resolves its actor and source message.';
comment on function public.claim_next_telegram_callback_event() is
  'Claims the oldest eligible Telegram callback with skip-locked worker concurrency.';
