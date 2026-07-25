alter table public.catalog_item_creation_requests
  add column details_reason text;

alter table public.catalog_item_creation_requests
  add check (
    details_reason is null
    or length(details_reason) between 1 and 1000
  );

create or replace function public.hydrate_catalog_item_creation_from_agent_draft()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_draft jsonb;
  v_attributes jsonb := '{}'::jsonb;
  v_source_event_id uuid;
  v_tracking_mode text;
  v_existing_name text;
  v_existing_attributes jsonb;
begin
  if new.status <> 'awaiting_details' then
    return new;
  end if;
  if new.details_reason is not null and new.sku is null then
    return new;
  end if;

  select
    line.match_evidence -> 'new_item',
    proposal.source_event_id
  into v_draft, v_source_event_id
  from public.proposal_lines as line
  join public.transaction_proposals as proposal
    on proposal.id = line.proposal_id
  where line.id = new.proposal_line_id;

  if jsonb_typeof(v_draft) <> 'object' then
    return new;
  end if;

  if jsonb_typeof(v_draft -> 'attributes') = 'array' then
    select coalesce(
      jsonb_object_agg(trim(attribute ->> 'key'), attribute ->> 'value'),
      '{}'::jsonb
    )
    into v_attributes
    from jsonb_array_elements(v_draft -> 'attributes') as attribute
    where nullif(trim(attribute ->> 'key'), '') is not null
      and nullif(trim(attribute ->> 'value'), '') is not null;
  end if;

  v_tracking_mode := nullif(lower(trim(v_draft ->> 'tracking_mode')), '');
  if v_tracking_mode not in ('simple', 'lot', 'serial') then
    v_tracking_mode := null;
  end if;

  new.name := coalesce(nullif(trim(v_draft ->> 'name'), ''), new.name);
  new.sku := coalesce(nullif(trim(v_draft ->> 'sku'), ''), new.sku);
  new.base_unit := coalesce(
    nullif(lower(trim(v_draft ->> 'base_unit')), ''),
    new.base_unit
  );
  new.tracking_mode := coalesce(
    v_tracking_mode::public.tracking_mode,
    new.tracking_mode
  );
  new.attributes := coalesce(new.attributes, '{}'::jsonb) || v_attributes;
  new.suggested_name := coalesce(new.name, new.suggested_name);
  new.suggested_sku := coalesce(new.sku, new.suggested_sku);
  new.suggested_base_unit := coalesce(new.base_unit, new.suggested_base_unit);
  new.suggested_tracking_mode := coalesce(
    new.tracking_mode,
    new.suggested_tracking_mode
  );
  new.details_reason := null;

  if new.sku is not null then
    select
      coalesce(variant.name, item.name),
      variant.attributes
    into v_existing_name, v_existing_attributes
    from public.item_variants as variant
    join public.items as item
      on item.organization_id = variant.organization_id
     and item.id = variant.item_id
    where variant.organization_id = new.organization_id
      and lower(variant.sku) = lower(trim(new.sku))
    limit 1;

    if found then
      new.details_reason := format(
        'SKU %s is already used by %s with attributes %s. '
        'The requested new variant has attributes %s. '
        'Reply with a different SKU, or cancel and choose the existing item if they are the same.',
        new.sku,
        v_existing_name,
        coalesce(v_existing_attributes, '{}'::jsonb)::text,
        coalesce(new.attributes, '{}'::jsonb)::text
      );
      new.sku := null;
      new.suggested_sku := null;
      new.status := 'awaiting_details';
      return new;
    end if;
  end if;

  if new.name is not null
    and new.sku is not null
    and new.base_unit is not null
    and new.tracking_mode = 'simple'
  then
    new.status := 'awaiting_confirmation';
    new.details_source_event_id := v_source_event_id;
  end if;
  return new;
end;
$$;

create or replace function public.prepare_catalog_item_creation_confirmation(
  p_request_id uuid,
  p_actor_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.catalog_item_creation_requests%rowtype;
  v_existing_name text;
  v_existing_attributes jsonb;
  v_message text;
begin
  select request.* into v_request
  from public.catalog_item_creation_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog item request was not found';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1
    from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_request.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot confirm this item request';
  end if;
  if v_request.status = 'completed' then
    return jsonb_build_object('ready', true);
  end if;
  if v_request.status = 'awaiting_details'
     and v_request.details_reason is not null then
    return jsonb_build_object(
      'ready', false,
      'request_id', v_request.id,
      'message', v_request.details_reason
    );
  end if;
  if v_request.status <> 'awaiting_confirmation' then
    raise exception using errcode = '22023', message = 'Catalog item request is not ready';
  end if;

  select
    coalesce(variant.name, item.name),
    variant.attributes
  into v_existing_name, v_existing_attributes
  from public.item_variants as variant
  join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
  where variant.organization_id = v_request.organization_id
    and lower(variant.sku) = lower(trim(v_request.sku))
  limit 1;

  if found then
    v_message := format(
      'SKU %s is already used by %s with attributes %s. '
      'The requested new variant has attributes %s. '
      'Reply with a different SKU, or cancel and choose the existing item if they are the same.',
      v_request.sku,
      v_existing_name,
      coalesce(v_existing_attributes, '{}'::jsonb)::text,
      coalesce(v_request.attributes, '{}'::jsonb)::text
    );
    update public.catalog_item_creation_requests
    set status = 'awaiting_details',
        sku = null,
        suggested_sku = null,
        details_reason = v_message,
        updated_at = now()
    where id = v_request.id;
    return jsonb_build_object(
      'ready', false,
      'request_id', v_request.id,
      'message', v_message
    );
  end if;

  return jsonb_build_object('ready', true);
end;
$$;

create or replace function public.get_catalog_item_creation_view(p_request_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  v_result jsonb;
begin
  select jsonb_build_object(
    'request_id', request.id,
    'status', request.status,
    'suggested_name', request.suggested_name,
    'suggested_sku', request.suggested_sku,
    'suggested_base_unit', request.suggested_base_unit,
    'suggested_tracking_mode', request.suggested_tracking_mode,
    'name', request.name,
    'sku', request.sku,
    'base_unit', request.base_unit,
    'tracking_mode', request.tracking_mode,
    'attributes', request.attributes,
    'details_reason', request.details_reason
  ) into v_result
  from public.catalog_item_creation_requests as request
  where request.id = p_request_id;
  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Catalog item request was not found';
  end if;
  return v_result;
end;
$$;

revoke all on function public.prepare_catalog_item_creation_confirmation(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.prepare_catalog_item_creation_confirmation(uuid, uuid)
  to service_role;

comment on function public.prepare_catalog_item_creation_confirmation(uuid, uuid) is
  'Atomically reopens catalog details when a proposed SKU became unavailable.';
