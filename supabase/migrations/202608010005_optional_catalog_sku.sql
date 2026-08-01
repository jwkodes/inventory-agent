alter table public.item_variants alter column sku drop not null;

alter table public.catalog_item_creation_requests
  add column sku_deferred boolean not null default false;

alter table public.catalog_item_creation_requests
  drop constraint catalog_item_creation_requests_check,
  add constraint catalog_item_creation_requests_ready_check check (
    status <> 'awaiting_confirmation'
    or (
      nullif(trim(name), '') is not null
      and (nullif(trim(sku), '') is not null or sku_deferred)
      and nullif(trim(base_unit), '') is not null
      and tracking_mode is not null
      and details_source_event_id is not null
    )
  ),
  add constraint catalog_item_creation_requests_deferred_sku_check check (
    not sku_deferred or sku is null
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

  select line.match_evidence -> 'new_item', proposal.source_event_id
  into v_draft, v_source_event_id
  from public.proposal_lines as line
  join public.transaction_proposals as proposal on proposal.id = line.proposal_id
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
  new.sku_deferred := new.sku is null;
  new.base_unit := coalesce(nullif(lower(trim(v_draft ->> 'base_unit')), ''), new.base_unit);
  new.tracking_mode := coalesce(v_tracking_mode::public.tracking_mode, new.tracking_mode);
  new.attributes := coalesce(new.attributes, '{}'::jsonb) || v_attributes;
  new.suggested_name := coalesce(new.name, new.suggested_name);
  new.suggested_sku := coalesce(new.sku, new.suggested_sku);
  new.suggested_base_unit := coalesce(new.base_unit, new.suggested_base_unit);
  new.suggested_tracking_mode := coalesce(new.tracking_mode, new.suggested_tracking_mode);
  new.details_reason := null;

  if new.sku is not null then
    select coalesce(variant.name, item.name), variant.attributes
    into v_existing_name, v_existing_attributes
    from public.item_variants as variant
    join public.items as item
      on item.organization_id = variant.organization_id and item.id = variant.item_id
    where variant.organization_id = new.organization_id
      and lower(variant.sku) = lower(trim(new.sku))
    limit 1;
    if found then
      new.details_reason := format(
        'SKU %s is already used by %s with attributes %s. '
        'The requested new variant has attributes %s. '
        'Reply with a different SKU, continue without one for now, or cancel.',
        new.sku,
        v_existing_name,
        coalesce(v_existing_attributes, '{}'::jsonb)::text,
        coalesce(new.attributes, '{}'::jsonb)::text
      );
      new.sku := null;
      new.suggested_sku := null;
      new.sku_deferred := false;
      new.status := 'awaiting_details';
      return new;
    end if;
  end if;

  if new.name is not null
    and (new.sku is not null or new.sku_deferred)
    and new.base_unit is not null
    and new.tracking_mode = 'simple'
  then
    new.status := 'awaiting_confirmation';
    new.details_source_event_id := v_source_event_id;
  end if;
  return new;
end;
$$;

create or replace function public.defer_catalog_item_creation_sku(
  p_request_id uuid,
  p_event_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.catalog_item_creation_requests%rowtype;
begin
  select request.* into v_request
  from public.catalog_item_creation_requests as request
  where request.id = p_request_id
  for update;
  if not found or v_request.status <> 'awaiting_details' then
    raise exception using errcode = '22023', message = 'Catalog request is not awaiting details';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.organization_id = v_request.organization_id
      and member.id = p_actor_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot edit this item request';
  end if;
  if not exists (
    select 1 from public.source_events as event
    where event.organization_id = v_request.organization_id
      and event.id = p_event_id
      and event.status = 'processing'
  ) then
    raise exception using errcode = '22023', message = 'Detail source event is invalid';
  end if;

  update public.catalog_item_creation_requests
  set name = coalesce(name, suggested_name),
      sku = null,
      suggested_sku = null,
      sku_deferred = true,
      base_unit = coalesce(base_unit, suggested_base_unit),
      tracking_mode = coalesce(tracking_mode, suggested_tracking_mode),
      details_source_event_id = p_event_id,
      details_reason = null,
      status = 'awaiting_confirmation',
      updated_at = now()
  where id = v_request.id;
  return v_request.id;
end;
$$;

create or replace function public.defer_catalog_batch_skus(
  p_batch_id uuid,
  p_event_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_batch public.catalog_batch_creation_requests%rowtype;
begin
  select batch.* into v_batch
  from public.catalog_batch_creation_requests as batch
  where batch.id = p_batch_id
  for update;
  if not found or v_batch.status <> 'awaiting_details' then
    raise exception using errcode = '22023', message = 'Catalog batch is not awaiting details';
  end if;
  if v_batch.requested_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.organization_id = v_batch.organization_id
      and member.id = p_actor_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot edit this catalog batch';
  end if;
  if not exists (
    select 1 from public.source_events as event
    where event.organization_id = v_batch.organization_id
      and event.id = p_event_id
      and event.status = 'processing'
  ) then
    raise exception using errcode = '22023', message = 'Batch detail source event is invalid';
  end if;

  update public.catalog_item_creation_requests
  set name = coalesce(name, suggested_name),
      sku_deferred = sku is null,
      base_unit = coalesce(base_unit, suggested_base_unit),
      tracking_mode = coalesce(tracking_mode, suggested_tracking_mode),
      details_source_event_id = p_event_id,
      details_reason = null,
      status = 'awaiting_confirmation',
      updated_at = now()
  where batch_id = v_batch.id;

  update public.catalog_batch_creation_requests
  set status = 'awaiting_confirmation',
      details_source_event_id = p_event_id,
      updated_at = now()
  where id = v_batch.id;
  return v_batch.id;
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
    'sku_deferred', request.sku_deferred,
    'base_unit', request.base_unit,
    'tracking_mode', request.tracking_mode,
    'attributes', request.attributes,
    'details_reason', request.details_reason,
    'line_number', line.line_number,
    'requested_quantity', line.requested_quantity,
    'requested_unit', line.requested_unit
  ) into v_result
  from public.catalog_item_creation_requests as request
  join public.proposal_lines as line
    on line.organization_id = request.organization_id
   and line.id = request.proposal_line_id
  where request.id = p_request_id;
  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Catalog item request was not found';
  end if;
  return v_result;
end;
$$;

create or replace function public.get_proposal_confirmation_view(p_proposal_id uuid)
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
    'proposal_id', proposal.id,
    'intent', proposal.intent,
    'lines', coalesce(jsonb_agg(jsonb_build_object(
      'proposal_line_id', line.id,
      'description', coalesce(nullif(trim(line.source_text), ''), line.extracted_description),
      'quantity', line.requested_quantity::text,
      'unit', line.requested_unit,
      'matched_label', case when variant.id is null then null
        else concat_ws(' · ', coalesce(variant.name, item.name), variant.sku) end,
      'match_decision', line.match_evidence ->> 'decision',
      'clarification_question', line.match_evidence ->> 'clarification_question',
      'show_candidates', coalesce((line.match_evidence ->> 'show_candidates')::boolean, false),
      'user_resolution', line.match_evidence ->> 'user_resolution',
      'new_item_preview', case
        when jsonb_typeof(line.match_evidence -> 'new_item') = 'object'
          and nullif(trim(line.match_evidence #>> '{new_item,name}'), '') is not null
          and nullif(trim(line.match_evidence #>> '{new_item,base_unit}'), '') is not null
          and line.match_evidence #>> '{new_item,tracking_mode}' = 'simple'
        then line.match_evidence -> 'new_item' else null end,
      'candidate_choices', case
        when variant.id is not null or line.match_evidence ->> 'user_resolution' = 'ignored'
        then '[]'::jsonb
        else coalesce((
          select jsonb_agg(jsonb_build_object(
            'item_variant_id', candidate.value ->> 'item_variant_id',
            'label', concat_ws(' · ', coalesce(
              candidate.value ->> 'variant_name', candidate.value ->> 'item_name',
              candidate.value ->> 'sku', 'Unknown item'
            ), candidate.value ->> 'sku')
          ) order by candidate.ordinality)
          from jsonb_array_elements(coalesce(line.match_evidence -> 'candidates', '[]'::jsonb))
            with ordinality as candidate(value, ordinality)
          where candidate.value ->> 'item_variant_id' is not null
        ), '[]'::jsonb) end
    ) order by line.line_number), '[]'::jsonb)
  ) into v_result
  from public.transaction_proposals as proposal
  join public.proposal_lines as line
    on line.organization_id = proposal.organization_id and line.proposal_id = proposal.id
  left join public.item_variants as variant
    on variant.organization_id = line.organization_id and variant.id = line.item_variant_id
  left join public.items as item
    on item.organization_id = variant.organization_id and item.id = variant.item_id
  where proposal.id = p_proposal_id
  group by proposal.id, proposal.intent;
  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Proposal confirmation view was not found';
  end if;
  return v_result;
end;
$$;

create or replace function public.confirm_catalog_batch_creation(
  p_batch_id uuid,
  p_actor_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_batch public.catalog_batch_creation_requests%rowtype;
  v_request record;
  v_conflict text;
  v_proposal_id uuid;
begin
  select batch.* into v_batch
  from public.catalog_batch_creation_requests as batch
  where batch.id = p_batch_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog batch was not found';
  end if;
  if v_batch.status = 'completed' then
    return jsonb_build_object('ready', true, 'proposal_id', v_batch.proposal_id);
  end if;
  if v_batch.requested_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.organization_id = v_batch.organization_id
      and member.id = p_actor_id and member.active and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot confirm this catalog batch';
  end if;
  if v_batch.status <> 'awaiting_confirmation' then
    raise exception using errcode = '22023', message = 'Catalog batch is not ready';
  end if;

  select format('SKU %s appears more than once in this batch.', request.sku)
  into v_conflict
  from public.catalog_item_creation_requests as request
  where request.batch_id = v_batch.id and request.sku is not null
  group by lower(request.sku), request.sku
  having count(*) > 1
  limit 1;
  if v_conflict is null then
    select format('SKU %s is already used by an existing catalog item.', request.sku)
    into v_conflict
    from public.catalog_item_creation_requests as request
    join public.item_variants as variant
      on variant.organization_id = request.organization_id
     and lower(variant.sku) = lower(request.sku)
    where request.batch_id = v_batch.id and request.sku is not null
    limit 1;
  end if;
  if v_conflict is not null then
    update public.catalog_batch_creation_requests
    set status = 'awaiting_details', updated_at = now()
    where id = v_batch.id;
    return jsonb_build_object('ready', false, 'message', v_conflict);
  end if;

  for v_request in
    select request.id
    from public.catalog_item_creation_requests as request
    join public.proposal_lines as line on line.id = request.proposal_line_id
    where request.batch_id = v_batch.id
    order by line.line_number
  loop
    v_proposal_id := public.confirm_catalog_item_creation(v_request.id, p_actor_id);
  end loop;
  update public.catalog_batch_creation_requests
  set status = 'completed', completed_at = now(), updated_at = now()
  where id = v_batch.id;
  return jsonb_build_object('ready', true, 'proposal_id', v_proposal_id);
end;
$$;

revoke all on function public.defer_catalog_item_creation_sku(uuid, uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.defer_catalog_batch_skus(uuid, uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.defer_catalog_item_creation_sku(uuid, uuid, uuid)
  to service_role;
grant execute on function public.defer_catalog_batch_skus(uuid, uuid, uuid)
  to service_role;

comment on column public.catalog_item_creation_requests.sku_deferred is
  'True only when the user explicitly chose to create this product without an SKU for now.';
