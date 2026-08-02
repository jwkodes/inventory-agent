alter table public.items
  add column description text,
  add constraint items_description_length
    check (description is null or length(description) <= 2000);

alter type public.processing_outcome_type
  add value if not exists 'catalog_item_edit_confirmation';

create type public.catalog_item_edit_status as enum (
  'awaiting_confirmation',
  'completed',
  'cancelled'
);

create table public.catalog_item_edit_requests (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  item_variant_id uuid not null,
  requested_by uuid not null,
  source_event_id uuid,
  chat_id bigint not null,
  status public.catalog_item_edit_status not null default 'awaiting_confirmation',
  reason text not null,
  before_values jsonb not null,
  after_values jsonb not null,
  confirmed_by uuid,
  confirmed_at timestamptz,
  cancelled_by uuid,
  cancelled_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id),
  foreign key (organization_id, requested_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, source_event_id)
    references public.source_events (organization_id, id) on delete set null,
  foreign key (organization_id, confirmed_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, cancelled_by)
    references public.organization_users (organization_id, id),
  unique (organization_id, source_event_id),
  check (length(trim(reason)) > 0 and length(reason) <= 1000),
  check (jsonb_typeof(before_values) = 'object'),
  check (jsonb_typeof(after_values) = 'object'),
  check (before_values <> after_values),
  check (
    (status = 'awaiting_confirmation' and confirmed_by is null and confirmed_at is null
      and cancelled_by is null and cancelled_at is null)
    or (status = 'completed' and confirmed_by is not null and confirmed_at is not null
      and cancelled_by is null and cancelled_at is null)
    or (status = 'cancelled' and cancelled_by is not null and cancelled_at is not null
      and confirmed_by is null and confirmed_at is null)
  )
);

create index catalog_item_edit_requests_variant_idx
  on public.catalog_item_edit_requests (organization_id, item_variant_id, created_at desc);

alter table public.catalog_item_edit_requests enable row level security;
revoke all on table public.catalog_item_edit_requests from public, anon, authenticated;
grant select on table public.catalog_item_edit_requests to service_role;

create or replace function public.begin_catalog_item_edit(
  p_item_variant_id uuid,
  p_actor_id uuid,
  p_source_event_id uuid,
  p_chat_id bigint,
  p_item_name text,
  p_variant_name text,
  p_sku text,
  p_description text,
  p_clear_fields text[],
  p_item_attribute_changes jsonb,
  p_variant_attribute_changes jsonb,
  p_reason text
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_variant public.item_variants%rowtype;
  v_item public.items%rowtype;
  v_existing public.catalog_item_edit_requests%rowtype;
  v_before jsonb;
  v_after jsonb;
  v_item_attributes jsonb;
  v_variant_attributes jsonb;
  v_change record;
begin
  select variant.* into v_variant
  from public.item_variants as variant
  where variant.id = p_item_variant_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog variant was not found';
  end if;

  select item.* into v_item
  from public.items as item
  where item.organization_id = v_variant.organization_id
    and item.id = v_variant.item_id
  for update;

  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_variant.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501',
      message = 'Only a manager or admin can update catalog products';
  end if;
  if not exists (
    select 1 from public.source_events as event
    where event.id = p_source_event_id
      and event.organization_id = v_variant.organization_id
  ) then
    raise exception using errcode = '42501', message = 'Source event is outside this organization';
  end if;

  select request.* into v_existing
  from public.catalog_item_edit_requests as request
  where request.organization_id = v_variant.organization_id
    and request.source_event_id = p_source_event_id
  for update;
  if found then
    return v_existing.id;
  end if;

  if p_clear_fields is null
    or p_item_attribute_changes is null
    or jsonb_typeof(p_item_attribute_changes) <> 'object'
    or p_variant_attribute_changes is null
    or jsonb_typeof(p_variant_attribute_changes) <> 'object'
  then
    raise exception using errcode = '22023', message = 'Catalog edit changes are invalid';
  end if;
  if exists (
    select 1 from unnest(p_clear_fields) as field_name
    where field_name not in ('variant_name', 'description')
  ) then
    raise exception using errcode = '22023', message = 'A requested field cannot be cleared';
  end if;
  if nullif(trim(p_reason), '') is null or length(p_reason) > 1000 then
    raise exception using errcode = '22023', message = 'A concise edit reason is required';
  end if;
  if p_item_name is not null
    and (nullif(trim(p_item_name), '') is null or length(trim(p_item_name)) > 200)
  then
    raise exception using errcode = '22023', message = 'Item name is invalid';
  end if;
  if p_variant_name is not null
    and (nullif(trim(p_variant_name), '') is null or length(trim(p_variant_name)) > 200)
  then
    raise exception using errcode = '22023', message = 'Variant name is invalid';
  end if;
  if p_sku is not null
    and (nullif(trim(p_sku), '') is null or length(trim(p_sku)) > 100)
  then
    raise exception using errcode = '22023', message = 'SKU is invalid';
  end if;
  if p_description is not null and length(p_description) > 2000 then
    raise exception using errcode = '22023', message = 'Description is too long';
  end if;

  v_item_attributes := v_item.attributes;
  for v_change in select key, value from jsonb_each(p_item_attribute_changes)
  loop
    if jsonb_typeof(v_change.value) = 'null' then
      v_item_attributes := v_item_attributes - v_change.key;
    elsif jsonb_typeof(v_change.value) = 'string' then
      v_item_attributes := jsonb_set(v_item_attributes, array[v_change.key], v_change.value, true);
    else
      raise exception using errcode = '22023',
        message = 'Attribute values must be strings or null';
    end if;
  end loop;

  v_variant_attributes := v_variant.attributes;
  for v_change in select key, value from jsonb_each(p_variant_attribute_changes)
  loop
    if jsonb_typeof(v_change.value) = 'null' then
      v_variant_attributes := v_variant_attributes - v_change.key;
    elsif jsonb_typeof(v_change.value) = 'string' then
      v_variant_attributes := jsonb_set(
        v_variant_attributes, array[v_change.key], v_change.value, true
      );
    else
      raise exception using errcode = '22023',
        message = 'Attribute values must be strings or null';
    end if;
  end loop;

  v_before := jsonb_build_object(
    'item_name', v_item.name,
    'variant_name', v_variant.name,
    'sku', v_variant.sku,
    'description', v_item.description,
    'item_attributes', v_item.attributes,
    'variant_attributes', v_variant.attributes
  );
  v_after := jsonb_build_object(
    'item_name', coalesce(trim(p_item_name), v_item.name),
    'variant_name', case
      when 'variant_name' = any(p_clear_fields) then null
      when p_variant_name is not null then trim(p_variant_name)
      else v_variant.name
    end,
    'sku', coalesce(trim(p_sku), v_variant.sku),
    'description', case
      when 'description' = any(p_clear_fields) then null
      when p_description is not null then nullif(trim(p_description), '')
      else v_item.description
    end,
    'item_attributes', v_item_attributes,
    'variant_attributes', v_variant_attributes
  );

  if v_before = v_after then
    raise exception using errcode = '22023', message = 'The catalog edit does not change anything';
  end if;
  if exists (
    select 1 from public.item_variants as other
    where other.organization_id = v_variant.organization_id
      and other.id <> v_variant.id
      and lower(trim(other.sku)) = lower(v_after ->> 'sku')
  ) then
    raise exception using errcode = '23505', message = 'That SKU is already in use';
  end if;

  insert into public.catalog_item_edit_requests (
    organization_id,
    item_variant_id,
    requested_by,
    source_event_id,
    chat_id,
    reason,
    before_values,
    after_values
  ) values (
    v_variant.organization_id,
    v_variant.id,
    p_actor_id,
    p_source_event_id,
    p_chat_id,
    trim(p_reason),
    v_before,
    v_after
  )
  returning id into v_existing.id;
  return v_existing.id;
end;
$$;

create or replace function public.get_catalog_item_edit_view(p_request_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select jsonb_build_object(
    'request_id', request.id,
    'item_variant_id', request.item_variant_id,
    'status', request.status,
    'reason', request.reason,
    'before_values', request.before_values,
    'after_values', request.after_values
  )
  from public.catalog_item_edit_requests as request
  where request.id = p_request_id;
$$;

create or replace function public.find_catalog_item_edit_by_source_event(
  p_source_event_id uuid
)
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select request.id
  from public.catalog_item_edit_requests as request
  where request.source_event_id = p_source_event_id;
$$;

create or replace function public.confirm_catalog_item_edit(
  p_request_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.catalog_item_edit_requests%rowtype;
  v_variant public.item_variants%rowtype;
  v_item public.items%rowtype;
  v_current jsonb;
begin
  select request.* into v_request
  from public.catalog_item_edit_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog edit request was not found';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_request.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501',
      message = 'Only a manager or admin can update catalog products';
  end if;
  if v_request.status = 'completed' then
    return v_request.id;
  end if;
  if v_request.status <> 'awaiting_confirmation' then
    raise exception using errcode = '22023', message = 'Catalog edit is no longer pending';
  end if;

  select variant.* into v_variant
  from public.item_variants as variant
  where variant.organization_id = v_request.organization_id
    and variant.id = v_request.item_variant_id
  for update;
  select item.* into v_item
  from public.items as item
  where item.organization_id = v_variant.organization_id
    and item.id = v_variant.item_id
  for update;
  v_current := jsonb_build_object(
    'item_name', v_item.name,
    'variant_name', v_variant.name,
    'sku', v_variant.sku,
    'description', v_item.description,
    'item_attributes', v_item.attributes,
    'variant_attributes', v_variant.attributes
  );
  if v_current <> v_request.before_values then
    raise exception using errcode = '40001',
      message = 'Catalog product changed after this review was created; request a fresh edit';
  end if;
  if exists (
    select 1 from public.item_variants as other
    where other.organization_id = v_request.organization_id
      and other.id <> v_variant.id
      and lower(trim(other.sku)) = lower(v_request.after_values ->> 'sku')
  ) then
    raise exception using errcode = '23505', message = 'That SKU is already in use';
  end if;

  update public.items
  set name = v_request.after_values ->> 'item_name',
      description = v_request.after_values ->> 'description',
      attributes = v_request.after_values -> 'item_attributes',
      updated_at = now()
  where organization_id = v_item.organization_id and id = v_item.id;
  update public.item_variants
  set name = v_request.after_values ->> 'variant_name',
      sku = v_request.after_values ->> 'sku',
      attributes = v_request.after_values -> 'variant_attributes',
      updated_at = now()
  where organization_id = v_variant.organization_id and id = v_variant.id;

  delete from public.inventory_variant_embeddings
  where organization_id = v_variant.organization_id
    and item_variant_id in (
      select affected.id from public.item_variants as affected
      where affected.organization_id = v_item.organization_id
        and affected.item_id = v_item.id
    );

  update public.catalog_item_edit_requests
  set status = 'completed',
      confirmed_by = p_actor_id,
      confirmed_at = now(),
      updated_at = now()
  where id = v_request.id;
  return v_request.id;
end;
$$;

create or replace function public.cancel_catalog_item_edit(
  p_request_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.catalog_item_edit_requests%rowtype;
begin
  select request.* into v_request
  from public.catalog_item_edit_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog edit request was not found';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_request.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501',
      message = 'Only a manager or admin can cancel catalog edits';
  end if;
  if v_request.status = 'cancelled' then
    return v_request.id;
  end if;
  if v_request.status <> 'awaiting_confirmation' then
    raise exception using errcode = '22023', message = 'Catalog edit is no longer pending';
  end if;
  update public.catalog_item_edit_requests
  set status = 'cancelled',
      cancelled_by = p_actor_id,
      cancelled_at = now(),
      updated_at = now()
  where id = v_request.id;
  return v_request.id;
end;
$$;

revoke all on function public.begin_catalog_item_edit(
  uuid, uuid, uuid, bigint, text, text, text, text, text[], jsonb, jsonb, text
) from public, anon, authenticated;
revoke all on function public.get_catalog_item_edit_view(uuid)
  from public, anon, authenticated;
revoke all on function public.find_catalog_item_edit_by_source_event(uuid)
  from public, anon, authenticated;
revoke all on function public.confirm_catalog_item_edit(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.cancel_catalog_item_edit(uuid, uuid)
  from public, anon, authenticated;

grant execute on function public.begin_catalog_item_edit(
  uuid, uuid, uuid, bigint, text, text, text, text, text[], jsonb, jsonb, text
) to service_role;
grant execute on function public.get_catalog_item_edit_view(uuid) to service_role;
grant execute on function public.find_catalog_item_edit_by_source_event(uuid) to service_role;
grant execute on function public.confirm_catalog_item_edit(uuid, uuid) to service_role;
grant execute on function public.cancel_catalog_item_edit(uuid, uuid) to service_role;

comment on table public.catalog_item_edit_requests is
  'Immutable before/after audit records for confirmed or cancelled catalog metadata edits.';
