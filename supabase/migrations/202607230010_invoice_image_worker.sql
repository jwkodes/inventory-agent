create index source_events_invoice_image_worker_idx
  on public.source_events (status, next_attempt_at, received_at, id)
  where provider = 'telegram' and event_type = 'invoice_image';

create or replace function public.claim_telegram_image_event(p_event_id uuid)
returns table (
  event_id uuid,
  organization_id uuid,
  organization_user_id uuid,
  location_id uuid,
  external_event_id text,
  chat_id bigint,
  telegram_user_id bigint,
  telegram_file_id text,
  telegram_file_unique_id text,
  media_type text,
  original_file_name text,
  file_size bigint,
  width integer,
  height integer,
  caption text
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_event public.source_events%rowtype;
  v_member_id uuid;
  v_location_id uuid;
  v_media jsonb;
  v_photos jsonb;
  v_chat_id_text text;
  v_user_id_text text;
  v_file_id text;
  v_file_unique_id text;
  v_media_type text;
  v_original_file_name text;
  v_file_size_text text;
  v_width_text text;
  v_height_text text;
  v_caption text;
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

  if v_event.provider <> 'telegram' or v_event.event_type <> 'invoice_image' then
    update public.source_events
    set status = 'failed',
        error_message = 'Event is not a Telegram invoice image',
        processed_at = now()
    where id = v_event.id;
    return;
  end if;

  v_photos := v_event.payload #> '{message,photo}';
  if jsonb_typeof(v_photos) = 'array' and jsonb_array_length(v_photos) > 0 then
    v_media := v_photos -> (jsonb_array_length(v_photos) - 1);
    v_media_type := 'image/jpeg';
  else
    v_media := v_event.payload #> '{message,document}';
    v_media_type := v_media ->> 'mime_type';
    v_original_file_name := nullif(v_media ->> 'file_name', '');
  end if;

  v_chat_id_text := v_event.payload #>> '{message,chat,id}';
  v_user_id_text := v_event.payload #>> '{message,from,id}';
  v_file_id := nullif(v_media ->> 'file_id', '');
  v_file_unique_id := nullif(v_media ->> 'file_unique_id', '');
  v_file_size_text := v_media ->> 'file_size';
  v_width_text := v_media ->> 'width';
  v_height_text := v_media ->> 'height';
  v_caption := nullif(trim(v_event.payload #>> '{message,caption}'), '');

  if v_file_id is null
     or v_media_type not in ('image/jpeg', 'image/png', 'image/webp')
     or v_chat_id_text is null
     or v_chat_id_text !~ '^-?[0-9]+$'
     or v_user_id_text is null
     or v_user_id_text !~ '^[0-9]+$'
     or (v_file_size_text is not null and v_file_size_text !~ '^[0-9]+$')
     or (v_width_text is not null and v_width_text !~ '^[0-9]+$')
     or (v_height_text is not null and v_height_text !~ '^[0-9]+$') then
    update public.source_events
    set status = 'failed',
        error_message = 'Telegram image is missing valid file, media, chat, or sender data',
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
    v_file_id,
    v_file_unique_id,
    v_media_type,
    v_original_file_name,
    v_file_size_text::bigint,
    v_width_text::integer,
    v_height_text::integer,
    v_caption;
end;
$$;

create or replace function public.claim_next_telegram_image_event()
returns table (
  event_id uuid,
  organization_id uuid,
  organization_user_id uuid,
  location_id uuid,
  external_event_id text,
  chat_id bigint,
  telegram_user_id bigint,
  telegram_file_id text,
  telegram_file_unique_id text,
  media_type text,
  original_file_name text,
  file_size bigint,
  width integer,
  height integer,
  caption text
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
    and source_event.event_type = 'invoice_image'
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
  from public.claim_telegram_image_event(v_event_id) as claimed;
end;
$$;

revoke all on function public.claim_telegram_image_event(uuid)
  from public, anon, authenticated;
revoke all on function public.claim_next_telegram_image_event()
  from public, anon, authenticated;
grant execute on function public.claim_telegram_image_event(uuid) to service_role;
grant execute on function public.claim_next_telegram_image_event() to service_role;

comment on function public.claim_telegram_image_event(uuid) is
  'Atomically validates and claims one persisted Telegram invoice image.';
comment on function public.claim_next_telegram_image_event() is
  'Claims the oldest eligible Telegram invoice image with skip-locked concurrency.';
