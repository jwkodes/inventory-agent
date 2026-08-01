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
        when v_suggested_sku is null
          then 'awaiting_details'::public.catalog_item_creation_status
        else 'awaiting_confirmation'::public.catalog_item_creation_status
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
        name = coalesce(public.catalog_item_creation_requests.name, excluded.name),
        sku = coalesce(public.catalog_item_creation_requests.sku, excluded.sku),
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
        updated_at = now();
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
