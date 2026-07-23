create type public.catalog_item_creation_status as enum (
  'awaiting_details',
  'awaiting_confirmation',
  'completed',
  'cancelled'
);

alter table public.proposal_lines
  add constraint proposal_lines_organization_id_id_key
  unique (organization_id, id);

create table public.catalog_item_creation_requests (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  proposal_line_id uuid not null unique,
  requested_by uuid not null,
  chat_id bigint not null,
  status public.catalog_item_creation_status not null default 'awaiting_details',
  suggested_name text,
  suggested_sku text,
  suggested_base_unit text not null default 'each',
  suggested_tracking_mode public.tracking_mode not null default 'simple',
  name text,
  sku text,
  base_unit text,
  tracking_mode public.tracking_mode,
  attributes jsonb not null default '{}'::jsonb,
  details_source_event_id uuid unique,
  created_item_id uuid,
  created_variant_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  foreign key (organization_id, proposal_line_id)
    references public.proposal_lines (organization_id, id) on delete cascade,
  foreign key (organization_id, requested_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, details_source_event_id)
    references public.source_events (organization_id, id),
  foreign key (organization_id, created_item_id)
    references public.items (organization_id, id),
  foreign key (organization_id, created_variant_id)
    references public.item_variants (organization_id, id),
  check (jsonb_typeof(attributes) = 'object'),
  check (
    status <> 'awaiting_confirmation'
    or (
      nullif(trim(name), '') is not null
      and nullif(trim(sku), '') is not null
      and nullif(trim(base_unit), '') is not null
      and tracking_mode is not null
      and details_source_event_id is not null
    )
  ),
  check (
    status <> 'completed'
    or (
      created_item_id is not null
      and created_variant_id is not null
      and completed_at is not null
    )
  )
);

create unique index catalog_item_creation_one_pending_details_idx
  on public.catalog_item_creation_requests (organization_id, requested_by, chat_id)
  where status = 'awaiting_details';

alter table public.catalog_item_creation_requests enable row level security;
grant select, insert, update, delete on public.catalog_item_creation_requests to service_role;

create or replace function public.browse_inventory_candidates(
  p_organization_id uuid,
  p_query text,
  p_limit integer default 5
)
returns table (
  item_variant_id uuid,
  item_id uuid,
  item_name text,
  variant_name text,
  sku text,
  base_unit text,
  tracking_mode public.tracking_mode,
  match_method public.match_method,
  match_score numeric,
  match_evidence jsonb
)
language sql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
  with input as (
    select
      trim(p_query) as raw_query,
      public.normalize_inventory_reference(p_query) as normalized_query,
      least(greatest(coalesce(p_limit, 5), 1), 20) as result_limit
  )
  select
    variant.id,
    item.id,
    item.name,
    variant.name,
    variant.sku,
    item.base_unit,
    item.tracking_mode,
    'text_search'::public.match_method,
    round(
      greatest(
        extensions.similarity(lower(item.name), lower(input.raw_query)),
        extensions.similarity(lower(coalesce(variant.name, '')), lower(input.raw_query)),
        extensions.similarity(
          public.normalize_inventory_reference(variant.sku),
          input.normalized_query
        )
      )::numeric * 0.85,
      7
    ),
    jsonb_build_object(
      'source', 'fallback_trigram',
      'item_name_similarity', extensions.similarity(lower(item.name), lower(input.raw_query)),
      'variant_name_similarity', extensions.similarity(
        lower(coalesce(variant.name, '')),
        lower(input.raw_query)
      ),
      'sku_similarity', extensions.similarity(
        public.normalize_inventory_reference(variant.sku),
        input.normalized_query
      )
    )
  from public.item_variants as variant
  join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
   and item.active
  cross join input
  where variant.organization_id = p_organization_id
    and variant.active
  order by 9 desc, item.name, variant.sku
  limit (select result_limit from input);
$$;

create or replace function public.show_existing_inventory_candidates(
  p_proposal_line_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_line public.proposal_lines%rowtype;
  v_proposal public.transaction_proposals%rowtype;
begin
  select line.* into v_line
  from public.proposal_lines as line
  where line.id = p_proposal_line_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Proposal line was not found';
  end if;

  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.id = v_line.proposal_id
  for update;
  if v_proposal.status <> 'pending_confirmation' or v_line.item_variant_id is not null then
    raise exception using errcode = '22023', message = 'Proposal line no longer needs matching';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_line.organization_id
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor is not an active member';
  end if;

  update public.proposal_lines
  set match_evidence = match_evidence || '{"show_candidates":true}'::jsonb
  where id = v_line.id;
  return v_line.proposal_id;
end;
$$;

create or replace function public.begin_catalog_item_creation(
  p_proposal_line_id uuid,
  p_actor_id uuid,
  p_chat_id bigint
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_line public.proposal_lines%rowtype;
  v_proposal public.transaction_proposals%rowtype;
  v_request public.catalog_item_creation_requests%rowtype;
  v_raw_line jsonb;
  v_suggested_sku text;
  v_suggested_unit text;
begin
  select line.* into v_line
  from public.proposal_lines as line
  where line.id = p_proposal_line_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Proposal line was not found';
  end if;
  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.id = v_line.proposal_id
  for update;
  if v_proposal.status <> 'pending_confirmation' or v_line.item_variant_id is not null then
    raise exception using errcode = '22023', message = 'Proposal line no longer needs matching';
  end if;
  if coalesce(v_line.match_evidence ->> 'decision', '') <> 'not_found' then
    raise exception using errcode = '22023', message = 'Only an unmatched line can create an item';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_line.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Only a manager or admin can create items';
  end if;

  select request.* into v_request
  from public.catalog_item_creation_requests as request
  where request.proposal_line_id = v_line.id
  for update;
  if found and v_request.status <> 'cancelled' then
    return v_request.id;
  end if;

  update public.catalog_item_creation_requests
  set status = 'cancelled',
      completed_at = now(),
      updated_at = now()
  where organization_id = v_line.organization_id
    and requested_by = p_actor_id
    and chat_id = p_chat_id
    and status = 'awaiting_details'
    and proposal_line_id <> v_line.id;

  select entry.value into v_raw_line
  from jsonb_array_elements(coalesce(v_proposal.raw_command -> 'lines', '[]'::jsonb))
    with ordinality as entry(value, position)
  where entry.position = v_line.line_number;

  if upper(coalesce(v_raw_line #>> '{item_reference,type}', '')) in (
    'SKU', 'PART_NUMBER'
  ) then
    v_suggested_sku := nullif(trim(v_raw_line #>> '{item_reference,value}'), '');
  end if;
  v_suggested_unit := case
    when lower(coalesce(trim(v_line.requested_unit), '')) in (
      '', 'unit', 'units', 'item', 'items'
    ) then 'each'
    else lower(trim(v_line.requested_unit))
  end;

  if v_request.id is not null then
    update public.catalog_item_creation_requests
    set requested_by = p_actor_id,
        chat_id = p_chat_id,
        status = 'awaiting_details',
        suggested_name = coalesce(v_line.extracted_description, v_line.source_text),
        suggested_sku = v_suggested_sku,
        suggested_base_unit = v_suggested_unit,
        name = null,
        sku = null,
        base_unit = null,
        tracking_mode = null,
        attributes = '{}'::jsonb,
        details_source_event_id = null,
        created_item_id = null,
        created_variant_id = null,
        completed_at = null,
        updated_at = now()
    where id = v_request.id;
    return v_request.id;
  end if;

  insert into public.catalog_item_creation_requests (
    organization_id,
    proposal_line_id,
    requested_by,
    chat_id,
    suggested_name,
    suggested_sku,
    suggested_base_unit
  )
  values (
    v_line.organization_id,
    v_line.id,
    p_actor_id,
    p_chat_id,
    coalesce(v_line.extracted_description, v_line.source_text),
    v_suggested_sku,
    v_suggested_unit
  )
  returning id into v_request.id;
  return v_request.id;
end;
$$;

create or replace function public.find_pending_catalog_item_creation(
  p_actor_id uuid,
  p_chat_id bigint
)
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select request.id
  from public.catalog_item_creation_requests as request
  join public.organization_users as member
    on member.organization_id = request.organization_id
   and member.id = request.requested_by
  where request.requested_by = p_actor_id
    and request.chat_id = p_chat_id
    and request.status = 'awaiting_details'
    and member.active
    and member.role in ('manager', 'admin')
  order by request.created_at
  limit 1;
$$;

create or replace function public.save_catalog_item_creation_details(
  p_request_id uuid,
  p_event_id uuid,
  p_actor_id uuid,
  p_name text,
  p_sku text,
  p_base_unit text,
  p_tracking_mode text,
  p_attributes jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.catalog_item_creation_requests%rowtype;
  v_tracking public.tracking_mode;
begin
  select request.* into v_request
  from public.catalog_item_creation_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog item request was not found';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_request.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot edit this item request';
  end if;
  if v_request.status = 'awaiting_confirmation'
    and v_request.details_source_event_id = p_event_id
  then
    return v_request.id;
  end if;
  if v_request.status <> 'awaiting_details' then
    raise exception using errcode = '22023', message = 'Catalog item request is not awaiting details';
  end if;
  if not exists (
    select 1 from public.source_events as event
    where event.id = p_event_id
      and event.organization_id = v_request.organization_id
      and event.status = 'processing'
  ) then
    raise exception using errcode = '22023', message = 'Detail source event is invalid';
  end if;
  if nullif(trim(p_name), '') is null
    or nullif(trim(p_sku), '') is null
    or nullif(trim(p_base_unit), '') is null
  then
    raise exception using errcode = '22023', message = 'Name, SKU, and base unit are required';
  end if;
  begin
    v_tracking := lower(trim(p_tracking_mode))::public.tracking_mode;
  exception when invalid_text_representation then
    raise exception using errcode = '22023', message = 'Tracking must be simple, lot, or serial';
  end;
  if jsonb_typeof(coalesce(p_attributes, '{}'::jsonb)) <> 'object' then
    raise exception using errcode = '22023', message = 'Attributes must be a JSON object';
  end if;
  if exists (
    select 1 from public.item_variants as variant
    where variant.organization_id = v_request.organization_id
      and lower(variant.sku) = lower(trim(p_sku))
  ) then
    raise exception using errcode = '23505', message = 'SKU already exists in this organization';
  end if;

  update public.catalog_item_creation_requests
  set name = trim(p_name),
      sku = trim(p_sku),
      base_unit = lower(trim(p_base_unit)),
      tracking_mode = v_tracking,
      attributes = coalesce(p_attributes, '{}'::jsonb),
      details_source_event_id = p_event_id,
      status = 'awaiting_confirmation',
      updated_at = now()
  where id = v_request.id;
  return v_request.id;
end;
$$;

create or replace function public.confirm_catalog_item_creation(
  p_request_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_request public.catalog_item_creation_requests%rowtype;
  v_line public.proposal_lines%rowtype;
  v_proposal public.transaction_proposals%rowtype;
  v_item_id uuid;
  v_variant_id uuid;
  v_factor numeric(24, 8);
begin
  select request.* into v_request
  from public.catalog_item_creation_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog item request was not found';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_request.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot confirm this item request';
  end if;
  select line.* into v_line
  from public.proposal_lines as line
  where line.id = v_request.proposal_line_id
  for update;
  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.id = v_line.proposal_id
  for update;

  if v_request.status = 'completed' then
    return v_line.proposal_id;
  end if;
  if v_request.status <> 'awaiting_confirmation' then
    raise exception using errcode = '22023', message = 'Catalog item request is not ready';
  end if;
  if v_proposal.status <> 'pending_confirmation' or v_line.item_variant_id is not null then
    raise exception using errcode = '22023', message = 'Proposal line no longer needs an item';
  end if;
  if v_request.tracking_mode <> 'simple' then
    raise exception using
      errcode = '0A000',
      message = 'Lot and serial item creation needs the tracking-detail workflow';
  end if;

  insert into public.items (
    organization_id, name, base_unit, tracking_mode
  )
  values (
    v_request.organization_id,
    v_request.name,
    v_request.base_unit,
    v_request.tracking_mode
  )
  returning id into v_item_id;

  insert into public.item_variants (
    organization_id, item_id, sku, attributes
  )
  values (
    v_request.organization_id,
    v_item_id,
    v_request.sku,
    v_request.attributes
  )
  returning id into v_variant_id;

  if v_line.requested_unit is null
    or lower(trim(v_line.requested_unit)) = lower(v_request.base_unit)
  then
    v_factor := 1;
  else
    select conversion.factor_to_base into v_factor
    from public.item_unit_conversions as conversion
    where conversion.organization_id = v_request.organization_id
      and conversion.item_variant_id = v_variant_id
      and lower(conversion.from_unit) = lower(trim(v_line.requested_unit));
  end if;
  if v_factor is null then
    raise exception using errcode = '22023', message = 'New item base unit does not match the request unit';
  end if;

  update public.proposal_lines
  set item_variant_id = v_variant_id,
      base_quantity_delta = v_line.requested_quantity * v_factor
        * case when v_proposal.intent = 'issue_stock' then -1 else 1 end,
      base_unit = v_request.base_unit,
      match_method = 'human_selected',
      match_score = 1,
      match_evidence = v_line.match_evidence || jsonb_build_object(
        'catalog_item_created', true,
        'created_item_variant_id', v_variant_id,
        'created_by', p_actor_id,
        'created_at', now()
      )
  where id = v_line.id;

  update public.catalog_item_creation_requests
  set status = 'completed',
      created_item_id = v_item_id,
      created_variant_id = v_variant_id,
      completed_at = now(),
      updated_at = now()
  where id = v_request.id;
  return v_line.proposal_id;
end;
$$;

create or replace function public.cancel_catalog_item_creation(
  p_request_id uuid,
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
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog item request was not found';
  end if;
  if v_request.requested_by <> p_actor_id then
    raise exception using errcode = '42501', message = 'Actor cannot cancel this item request';
  end if;
  if v_request.status = 'completed' then
    raise exception using errcode = '22023', message = 'A completed item request cannot be cancelled';
  end if;
  update public.catalog_item_creation_requests
  set status = 'cancelled', completed_at = now(), updated_at = now()
  where id = v_request.id;
  return v_request.id;
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
    'attributes', request.attributes
  ) into v_result
  from public.catalog_item_creation_requests as request
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
    'lines', coalesce(
      jsonb_agg(
        jsonb_build_object(
          'proposal_line_id', line.id,
          'description', coalesce(line.extracted_description, line.source_text),
          'quantity', line.requested_quantity::text,
          'unit', line.requested_unit,
          'matched_label', case
            when variant.id is null then null
            else coalesce(variant.name, item.name) || ' · ' || variant.sku
          end,
          'match_decision', line.match_evidence ->> 'decision',
          'show_candidates', coalesce(
            (line.match_evidence ->> 'show_candidates')::boolean,
            false
          ),
          'candidate_choices', case
            when variant.id is not null then '[]'::jsonb
            else coalesce(
              (
                select jsonb_agg(
                  jsonb_build_object(
                    'item_variant_id', candidate.value ->> 'item_variant_id',
                    'label',
                      coalesce(
                        candidate.value ->> 'variant_name',
                        candidate.value ->> 'item_name',
                        candidate.value ->> 'sku',
                        'Unknown item'
                      ) || case
                        when candidate.value ->> 'sku' is null then ''
                        else ' · ' || (candidate.value ->> 'sku')
                      end
                  )
                  order by candidate.ordinality
                )
                from jsonb_array_elements(
                  coalesce(line.match_evidence -> 'candidates', '[]'::jsonb)
                ) with ordinality as candidate(value, ordinality)
                where candidate.value ->> 'item_variant_id' is not null
              ),
              '[]'::jsonb
            )
          end
        )
        order by line.line_number
      ),
      '[]'::jsonb
    )
  ) into v_result
  from public.transaction_proposals as proposal
  join public.proposal_lines as line
    on line.organization_id = proposal.organization_id
   and line.proposal_id = proposal.id
  left join public.item_variants as variant
    on variant.organization_id = line.organization_id
   and variant.id = line.item_variant_id
  left join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
  where proposal.id = p_proposal_id
  group by proposal.id, proposal.intent;
  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Proposal confirmation view was not found';
  end if;
  return v_result;
end;
$$;

alter table public.processing_outbox
  drop constraint processing_outbox_aggregate_check,
  add constraint processing_outbox_aggregate_check check (
    (
      outcome_type in (
        'proposal_ready',
        'transaction_applied',
        'catalog_item_details_required',
        'catalog_item_confirmation',
        'reversal_reason_required',
        'reversal_confirmation'
      )
      and aggregate_id is not null
    )
    or (
      outcome_type not in (
        'proposal_ready',
        'transaction_applied',
        'catalog_item_details_required',
        'catalog_item_confirmation',
        'reversal_reason_required',
        'reversal_confirmation'
      )
      and aggregate_id is null
    )
  );

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
    where proposal.organization_id = p_organization_id and proposal.id = p_aggregate_id
  ) then
    raise exception using errcode = '22023', message = 'Proposal is not in the organization';
  end if;
  if p_outcome_type = 'transaction_applied' and not exists (
    select 1 from public.inventory_transactions as transaction
    where transaction.organization_id = p_organization_id
      and transaction.id = p_aggregate_id
      and transaction.status = 'applied'
  ) then
    raise exception using errcode = '22023', message = 'Applied transaction is not in the organization';
  end if;
  if p_outcome_type in ('catalog_item_details_required', 'catalog_item_confirmation')
    and not exists (
      select 1 from public.catalog_item_creation_requests as request
      where request.organization_id = p_organization_id
        and request.id = p_aggregate_id
        and request.chat_id = p_chat_id
        and (
          (p_outcome_type = 'catalog_item_details_required'
            and request.status = 'awaiting_details')
          or
          (p_outcome_type = 'catalog_item_confirmation'
            and request.status = 'awaiting_confirmation')
        )
    )
  then
    raise exception using errcode = '22023', message = 'Catalog request state does not match outcome';
  end if;
  if p_outcome_type = 'callback_notice'
    and nullif(trim(p_payload ->> 'message'), '') is null
  then
    raise exception using errcode = '22023', message = 'Callback notice requires a message';
  end if;
  if p_outcome_type = 'reversal_reason_required' and not exists (
    select 1 from public.transaction_reversal_requests as request
    where request.organization_id = p_organization_id
      and request.id = p_aggregate_id
      and request.status = 'awaiting_reason'
      and request.chat_id = p_chat_id
  ) then
    raise exception using errcode = '22023', message = 'Pending reversal reason request does not match organization or chat';
  end if;
  if p_outcome_type = 'reversal_confirmation' and not exists (
    select 1 from public.transaction_reversal_requests as request
    where request.organization_id = p_organization_id
      and request.id = p_aggregate_id
      and request.status = 'awaiting_confirmation'
      and request.chat_id = p_chat_id
      and request.reason = nullif(trim(p_payload ->> 'reason'), '')
  ) then
    raise exception using errcode = '22023', message = 'Pending reversal confirmation does not match organization, chat, or reason';
  end if;

  insert into public.processing_outbox (
    organization_id, source_event_id, outcome_type, aggregate_id, chat_id, payload
  )
  values (
    p_organization_id, p_source_event_id, p_outcome_type,
    p_aggregate_id, p_chat_id, coalesce(p_payload, '{}'::jsonb)
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

revoke all on function public.browse_inventory_candidates(uuid, text, integer)
  from public, anon, authenticated;
revoke all on function public.show_existing_inventory_candidates(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.begin_catalog_item_creation(uuid, uuid, bigint)
  from public, anon, authenticated;
revoke all on function public.find_pending_catalog_item_creation(uuid, bigint)
  from public, anon, authenticated;
revoke all on function public.save_catalog_item_creation_details(
  uuid, uuid, uuid, text, text, text, text, jsonb
) from public, anon, authenticated;
revoke all on function public.confirm_catalog_item_creation(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.cancel_catalog_item_creation(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.get_catalog_item_creation_view(uuid)
  from public, anon, authenticated;

grant execute on function public.browse_inventory_candidates(uuid, text, integer)
  to service_role;
grant execute on function public.show_existing_inventory_candidates(uuid, uuid)
  to service_role;
grant execute on function public.begin_catalog_item_creation(uuid, uuid, bigint)
  to service_role;
grant execute on function public.find_pending_catalog_item_creation(uuid, bigint)
  to service_role;
grant execute on function public.save_catalog_item_creation_details(
  uuid, uuid, uuid, text, text, text, text, jsonb
) to service_role;
grant execute on function public.confirm_catalog_item_creation(uuid, uuid)
  to service_role;
grant execute on function public.cancel_catalog_item_creation(uuid, uuid)
  to service_role;
grant execute on function public.get_catalog_item_creation_view(uuid)
  to service_role;

comment on table public.catalog_item_creation_requests is
  'Durable manager/admin workflow for resolving an unmatched proposal line by creating a catalog item.';
