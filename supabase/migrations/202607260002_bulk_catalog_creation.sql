alter type public.processing_outcome_type
  add value if not exists 'catalog_batch_details_required';
alter type public.processing_outcome_type
  add value if not exists 'catalog_batch_confirmation';

create or replace function public.add_default_variant_unit_conversions()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.item_unit_conversions (
    organization_id,
    item_variant_id,
    from_unit,
    factor_to_base
  )
  select
    new.organization_id,
    new.id,
    alias.from_unit,
    1
  from (
    values
      ('unit'), ('units'), ('item'), ('items'),
      ('pc'), ('pcs'), ('piece'), ('pieces')
  ) as alias(from_unit)
  on conflict (organization_id, item_variant_id, from_unit) do nothing;
  return new;
end;
$$;

insert into public.item_unit_conversions (
  organization_id,
  item_variant_id,
  from_unit,
  factor_to_base
)
select
  variant.organization_id,
  variant.id,
  alias.from_unit,
  1
from public.item_variants as variant
cross join (
  values ('pc'), ('pcs'), ('piece'), ('pieces')
) as alias(from_unit)
on conflict (organization_id, item_variant_id, from_unit) do nothing;

create type public.catalog_batch_creation_status as enum (
  'awaiting_details',
  'awaiting_confirmation',
  'completed',
  'cancelled'
);

create table public.catalog_batch_creation_requests (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  proposal_id uuid not null unique,
  requested_by uuid not null,
  chat_id bigint not null,
  status public.catalog_batch_creation_status not null default 'awaiting_details',
  details_source_event_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  foreign key (organization_id, proposal_id)
    references public.transaction_proposals (organization_id, id) on delete cascade,
  foreign key (organization_id, requested_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, details_source_event_id)
    references public.source_events (organization_id, id) on delete set null,
  check (
    (status in ('awaiting_details', 'awaiting_confirmation') and completed_at is null)
    or (status in ('completed', 'cancelled') and completed_at is not null)
  )
);

alter table public.catalog_item_creation_requests
  add column batch_id uuid references public.catalog_batch_creation_requests (id)
    on delete cascade;

alter table public.catalog_item_creation_requests
  drop constraint catalog_item_creation_requests_details_source_event_id_key;

drop index public.catalog_item_creation_one_pending_details_idx;
create unique index catalog_item_creation_one_pending_details_idx
  on public.catalog_item_creation_requests (organization_id, requested_by, chat_id)
  where status = 'awaiting_details' and batch_id is null;

create index catalog_item_creation_batch_idx
  on public.catalog_item_creation_requests (batch_id, created_at, id)
  where batch_id is not null;

create unique index catalog_batch_creation_pending_idx
  on public.catalog_batch_creation_requests (organization_id, requested_by, chat_id)
  where status = 'awaiting_details';

alter table public.catalog_batch_creation_requests enable row level security;
revoke all on table public.catalog_batch_creation_requests from public, anon, authenticated;
grant select, insert, update, delete
  on public.catalog_batch_creation_requests to service_role;

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
    and request.batch_id is null
    and member.active
    and member.role in ('manager', 'admin')
  order by request.created_at
  limit 1;
$$;

create or replace function public.begin_catalog_batch_creation(
  p_proposal_id uuid,
  p_actor_id uuid,
  p_chat_id bigint
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_proposal public.transaction_proposals%rowtype;
  v_batch public.catalog_batch_creation_requests%rowtype;
  v_line public.proposal_lines%rowtype;
  v_request_id uuid;
  v_raw_line jsonb;
  v_suggested_sku text;
  v_suggested_unit text;
  v_count integer := 0;
begin
  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.id = p_proposal_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Proposal was not found';
  end if;
  if v_proposal.status <> 'pending_confirmation' then
    raise exception using errcode = '22023', message = 'Proposal is not pending';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_proposal.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Only a manager or admin can create items';
  end if;

  select batch.* into v_batch
  from public.catalog_batch_creation_requests as batch
  where batch.proposal_id = v_proposal.id
  for update;
  if found and v_batch.status <> 'cancelled' then
    return v_batch.id;
  end if;

  update public.catalog_batch_creation_requests
  set status = 'cancelled', completed_at = now(), updated_at = now()
  where organization_id = v_proposal.organization_id
    and requested_by = p_actor_id
    and chat_id = p_chat_id
    and status in ('awaiting_details', 'awaiting_confirmation')
    and proposal_id <> v_proposal.id;

  update public.catalog_item_creation_requests
  set status = 'cancelled', completed_at = now(), updated_at = now()
  where organization_id = v_proposal.organization_id
    and requested_by = p_actor_id
    and chat_id = p_chat_id
    and status = 'awaiting_details'
    and batch_id is null;

  if v_batch.id is null then
    insert into public.catalog_batch_creation_requests (
      organization_id, proposal_id, requested_by, chat_id
    )
    values (
      v_proposal.organization_id, v_proposal.id, p_actor_id, p_chat_id
    )
    returning * into v_batch;
  else
    update public.catalog_batch_creation_requests
    set requested_by = p_actor_id,
        chat_id = p_chat_id,
        status = 'awaiting_details',
        details_source_event_id = null,
        completed_at = null,
        updated_at = now()
    where id = v_batch.id
    returning * into v_batch;
  end if;

  for v_line in
    select line.*
    from public.proposal_lines as line
    where line.proposal_id = v_proposal.id
      and line.item_variant_id is null
      and coalesce(line.match_evidence ->> 'decision', '') = 'not_found'
    order by line.line_number
  loop
    v_count := v_count + 1;
    select entry.value into v_raw_line
    from jsonb_array_elements(coalesce(v_proposal.raw_command -> 'lines', '[]'::jsonb))
      with ordinality as entry(value, position)
    where entry.position = v_line.line_number;

    v_suggested_sku := null;
    if upper(coalesce(v_raw_line #>> '{item_reference,type}', '')) in (
      'SKU', 'PART_NUMBER'
    ) then
      v_suggested_sku := nullif(trim(v_raw_line #>> '{item_reference,value}'), '');
    end if;
    v_suggested_unit := case
      when lower(coalesce(trim(v_line.requested_unit), '')) in (
        '', 'unit', 'units', 'item', 'items', 'pcs', 'pc', 'piece', 'pieces'
      ) then 'each'
      else lower(trim(v_line.requested_unit))
    end;

    insert into public.catalog_item_creation_requests (
      organization_id,
      proposal_line_id,
      requested_by,
      chat_id,
      batch_id,
      status,
      suggested_name,
      suggested_sku,
      suggested_base_unit,
      name,
      sku,
      base_unit,
      tracking_mode,
      details_source_event_id
    )
    values (
      v_proposal.organization_id,
      v_line.id,
      p_actor_id,
      p_chat_id,
      v_batch.id,
      case
        when v_suggested_sku is null then 'awaiting_details'
        else 'awaiting_confirmation'
      end,
      coalesce(v_line.extracted_description, v_line.source_text),
      v_suggested_sku,
      v_suggested_unit,
      coalesce(v_line.extracted_description, v_line.source_text),
      v_suggested_sku,
      v_suggested_unit,
      'simple',
      case when v_suggested_sku is null then null else v_proposal.source_event_id end
    )
    on conflict (proposal_line_id) do update
    set requested_by = excluded.requested_by,
        chat_id = excluded.chat_id,
        batch_id = excluded.batch_id,
        status = case
          when public.catalog_item_creation_requests.sku is not null
            then 'awaiting_confirmation'::public.catalog_item_creation_status
          when excluded.suggested_sku is not null
            then 'awaiting_confirmation'::public.catalog_item_creation_status
          else 'awaiting_details'::public.catalog_item_creation_status
        end,
        suggested_name = excluded.suggested_name,
        suggested_sku = coalesce(
          public.catalog_item_creation_requests.suggested_sku,
          excluded.suggested_sku
        ),
        suggested_base_unit = excluded.suggested_base_unit,
        name = coalesce(
          public.catalog_item_creation_requests.name,
          excluded.name
        ),
        sku = coalesce(
          public.catalog_item_creation_requests.sku,
          excluded.sku
        ),
        base_unit = coalesce(
          public.catalog_item_creation_requests.base_unit,
          excluded.base_unit
        ),
        tracking_mode = coalesce(
          public.catalog_item_creation_requests.tracking_mode,
          excluded.tracking_mode
        ),
        details_source_event_id = case
          when coalesce(
            public.catalog_item_creation_requests.sku,
            excluded.sku
          ) is null then null
          else coalesce(
            public.catalog_item_creation_requests.details_source_event_id,
            excluded.details_source_event_id
          )
        end,
        completed_at = null,
        updated_at = now()
    returning id into v_request_id;
  end loop;

  if v_count < 2 then
    raise exception using
      errcode = '22023',
      message = 'Bulk catalog creation requires at least two unmatched lines';
  end if;

  update public.catalog_batch_creation_requests
  set status = case
        when exists (
          select 1
          from public.catalog_item_creation_requests as request
          where request.batch_id = v_batch.id
            and request.status = 'awaiting_details'
        ) then 'awaiting_details'::public.catalog_batch_creation_status
        else 'awaiting_confirmation'::public.catalog_batch_creation_status
      end,
      updated_at = now()
  where id = v_batch.id;
  return v_batch.id;
end;
$$;

create or replace function public.find_pending_catalog_batch_creation(
  p_actor_id uuid,
  p_chat_id bigint
)
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select batch.id
  from public.catalog_batch_creation_requests as batch
  join public.organization_users as member
    on member.organization_id = batch.organization_id
   and member.id = batch.requested_by
  where batch.requested_by = p_actor_id
    and batch.chat_id = p_chat_id
    and batch.status = 'awaiting_details'
    and member.active
    and member.role in ('manager', 'admin')
  order by batch.created_at
  limit 1;
$$;

create or replace function public.get_catalog_batch_creation_view(p_batch_id uuid)
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
    'batch_id', batch.id,
    'proposal_id', batch.proposal_id,
    'status', batch.status,
    'items', coalesce(
      jsonb_agg(
        jsonb_build_object(
          'request_id', request.id,
          'line_number', line.line_number,
          'requested_quantity', line.requested_quantity::text,
          'requested_unit', line.requested_unit,
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
        )
        order by line.line_number
      ),
      '[]'::jsonb
    )
  ) into v_result
  from public.catalog_batch_creation_requests as batch
  join public.catalog_item_creation_requests as request
    on request.batch_id = batch.id
  join public.proposal_lines as line
    on line.id = request.proposal_line_id
  where batch.id = p_batch_id
  group by batch.id;

  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Catalog batch was not found';
  end if;
  return v_result;
end;
$$;

create or replace function public.save_catalog_batch_creation_draft(
  p_batch_id uuid,
  p_event_id uuid,
  p_actor_id uuid,
  p_items jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_batch public.catalog_batch_creation_requests%rowtype;
  v_item jsonb;
  v_request public.catalog_item_creation_requests%rowtype;
  v_tracking public.tracking_mode;
begin
  if jsonb_typeof(p_items) <> 'array' then
    raise exception using errcode = '22023', message = 'Batch item details must be an array';
  end if;
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

  for v_item in select value from jsonb_array_elements(p_items)
  loop
    select request.* into v_request
    from public.catalog_item_creation_requests as request
    where request.id = (v_item ->> 'request_id')::uuid
      and request.batch_id = v_batch.id
    for update;
    if not found then
      raise exception using errcode = '22023', message = 'Batch item request is invalid';
    end if;
    begin
      v_tracking := coalesce(
        nullif(v_item ->> 'tracking_mode', '')::public.tracking_mode,
        v_request.tracking_mode,
        v_request.suggested_tracking_mode
      );
    exception when invalid_text_representation then
      raise exception using errcode = '22023', message = 'Tracking mode is invalid';
    end;

    update public.catalog_item_creation_requests
    set name = coalesce(nullif(trim(v_item ->> 'name'), ''), name, suggested_name),
        sku = coalesce(nullif(trim(v_item ->> 'sku'), ''), sku, suggested_sku),
        base_unit = coalesce(
          nullif(lower(trim(v_item ->> 'base_unit')), ''),
          base_unit,
          suggested_base_unit
        ),
        tracking_mode = v_tracking,
        attributes = attributes || coalesce(v_item -> 'attributes', '{}'::jsonb),
        details_source_event_id = p_event_id,
        details_reason = null,
        updated_at = now()
    where id = v_request.id;

    update public.catalog_item_creation_requests
    set status = case
          when nullif(trim(name), '') is not null
            and nullif(trim(sku), '') is not null
            and nullif(trim(base_unit), '') is not null
            and tracking_mode = 'simple'
          then 'awaiting_confirmation'::public.catalog_item_creation_status
          else 'awaiting_details'::public.catalog_item_creation_status
        end
    where id = v_request.id;
  end loop;

  update public.catalog_batch_creation_requests
  set status = case
      when exists (
          select 1 from public.catalog_item_creation_requests as request
          where request.batch_id = v_batch.id
            and request.status = 'awaiting_details'
        ) or exists (
          select 1
          from public.catalog_item_creation_requests as request
          where request.batch_id = v_batch.id
            and request.sku is not null
          group by lower(request.sku)
          having count(*) > 1
        ) then 'awaiting_details'::public.catalog_batch_creation_status
        else 'awaiting_confirmation'::public.catalog_batch_creation_status
      end,
      details_source_event_id = p_event_id,
      updated_at = now()
  where id = v_batch.id;
  return v_batch.id;
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
    return jsonb_build_object(
      'ready', true,
      'proposal_id', v_batch.proposal_id
    );
  end if;
  if v_batch.requested_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.organization_id = v_batch.organization_id
      and member.id = p_actor_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot confirm this catalog batch';
  end if;
  if v_batch.status <> 'awaiting_confirmation' then
    raise exception using errcode = '22023', message = 'Catalog batch is not ready';
  end if;

  select format('SKU %s appears more than once in this batch.', request.sku)
  into v_conflict
  from public.catalog_item_creation_requests as request
  where request.batch_id = v_batch.id
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
    where request.batch_id = v_batch.id
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

create or replace function public.cancel_catalog_batch_creation(
  p_batch_id uuid,
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
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog batch was not found';
  end if;
  if v_batch.requested_by <> p_actor_id then
    raise exception using errcode = '42501', message = 'Actor cannot cancel this catalog batch';
  end if;
  if v_batch.status = 'completed' then
    raise exception using errcode = '22023', message = 'A completed batch cannot be cancelled';
  end if;

  update public.catalog_item_creation_requests
  set status = 'cancelled', completed_at = now(), updated_at = now()
  where batch_id = v_batch.id
    and status <> 'completed';
  update public.catalog_batch_creation_requests
  set status = 'cancelled', completed_at = now(), updated_at = now()
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
    'base_unit', request.base_unit,
    'tracking_mode', request.tracking_mode,
    'attributes', request.attributes,
    'details_reason', request.details_reason,
    'line_number', line.line_number,
    'requested_quantity', line.requested_quantity::text,
    'requested_unit', line.requested_unit
  ) into v_result
  from public.catalog_item_creation_requests as request
  join public.proposal_lines as line on line.id = request.proposal_line_id
  where request.id = p_request_id;
  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Catalog item request was not found';
  end if;
  return v_result;
end;
$$;

revoke all on function public.begin_catalog_batch_creation(uuid, uuid, bigint)
  from public, anon, authenticated;
revoke all on function public.find_pending_catalog_batch_creation(uuid, bigint)
  from public, anon, authenticated;
revoke all on function public.get_catalog_batch_creation_view(uuid)
  from public, anon, authenticated;
revoke all on function public.save_catalog_batch_creation_draft(
  uuid, uuid, uuid, jsonb
) from public, anon, authenticated;
revoke all on function public.confirm_catalog_batch_creation(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.cancel_catalog_batch_creation(uuid, uuid)
  from public, anon, authenticated;

grant execute on function public.begin_catalog_batch_creation(uuid, uuid, bigint)
  to service_role;
grant execute on function public.find_pending_catalog_batch_creation(uuid, bigint)
  to service_role;
grant execute on function public.get_catalog_batch_creation_view(uuid)
  to service_role;
grant execute on function public.save_catalog_batch_creation_draft(
  uuid, uuid, uuid, jsonb
) to service_role;
grant execute on function public.confirm_catalog_batch_creation(uuid, uuid)
  to service_role;
grant execute on function public.cancel_catalog_batch_creation(uuid, uuid)
  to service_role;

comment on table public.catalog_batch_creation_requests is
  'One reviewable catalog-creation batch for multiple unmatched proposal lines.';
