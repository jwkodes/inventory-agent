create or replace function public.mark_proposal_line_as_new_item(
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
  if v_proposal.status <> 'pending_confirmation'
    or v_line.item_variant_id is not null
    or coalesce(v_line.match_evidence ->> 'decision', '') <> 'not_found'
    or coalesce(v_line.match_evidence ->> 'user_resolution', '') = 'ignored'
  then
    raise exception using
      errcode = '22023',
      message = 'Proposal line cannot be marked as a new item';
  end if;
  if not exists (
    select 1
    from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_line.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using
      errcode = '42501',
      message = 'Only a manager or admin can create catalog items';
  end if;

  update public.proposal_lines
  set extracted_description = coalesce(nullif(trim(source_text), ''), extracted_description),
      match_evidence = match_evidence || jsonb_build_object(
        'user_resolution', 'add_new',
        'resolution_by', p_actor_id,
        'resolution_at', now()
      )
  where id = v_line.id;
  return v_line.proposal_id;
end;
$$;

create or replace function public.mark_all_unmatched_proposal_lines_as_new(
  p_proposal_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_proposal public.transaction_proposals%rowtype;
  v_new_line_count integer;
begin
  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.id = p_proposal_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Proposal was not found';
  end if;
  if v_proposal.status <> 'pending_confirmation' then
    raise exception using errcode = '22023', message = 'Proposal is no longer pending';
  end if;
  if not exists (
    select 1
    from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_proposal.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using
      errcode = '42501',
      message = 'Only a manager or admin can create catalog items';
  end if;

  update public.proposal_lines
  set extracted_description = coalesce(nullif(trim(source_text), ''), extracted_description),
      match_evidence = match_evidence || jsonb_build_object(
        'user_resolution', 'add_new',
        'resolution_by', p_actor_id,
        'resolution_at', now()
      )
  where proposal_id = v_proposal.id
    and item_variant_id is null
    and coalesce(match_evidence ->> 'decision', '') = 'not_found'
    and coalesce((match_evidence ->> 'show_candidates')::boolean, false) = false
    and coalesce(match_evidence ->> 'user_resolution', '') <> 'ignored';

  select count(*) into v_new_line_count
  from public.proposal_lines as line
  where line.proposal_id = v_proposal.id
    and line.item_variant_id is null
    and line.match_evidence ->> 'user_resolution' = 'add_new';
  if v_new_line_count < 2 then
    raise exception using
      errcode = '22023',
      message = 'Bulk catalog creation requires at least two selected new items';
  end if;
  return v_proposal.id;
end;
$$;

create or replace function public.ignore_inventory_proposal_line(
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
  v_active_line_count integer;
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
  if v_proposal.status <> 'pending_confirmation'
    or v_line.item_variant_id is not null
  then
    raise exception using
      errcode = '22023',
      message = 'Proposal line cannot be ignored';
  end if;
  if not exists (
    select 1
    from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_line.organization_id
      and member.active
  ) then
    raise exception using
      errcode = '42501',
      message = 'Actor is not an active organization member';
  end if;

  select count(*) into v_active_line_count
  from public.proposal_lines as line
  where line.proposal_id = v_proposal.id
    and coalesce(line.match_evidence ->> 'user_resolution', '') <> 'ignored';
  if v_active_line_count <= 1 then
    raise exception using
      errcode = '22023',
      message = 'The final active proposal line cannot be ignored';
  end if;

  update public.proposal_lines
  set item_variant_id = null,
      lot_id = null,
      serial_id = null,
      base_quantity_delta = null,
      base_unit = null,
      match_method = null,
      match_score = null,
      match_evidence = match_evidence || jsonb_build_object(
        'decision', 'ignored',
        'user_resolution', 'ignored',
        'resolution_by', p_actor_id,
        'resolution_at', now(),
        'resolution_reason', 'Excluded by user during proposal review'
      )
  where id = v_line.id;
  return v_line.proposal_id;
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
          'description', coalesce(nullif(trim(line.source_text), ''), line.extracted_description),
          'quantity', line.requested_quantity::text,
          'unit', line.requested_unit,
          'matched_label', case
            when variant.id is null then null
            else coalesce(variant.name, item.name) || ' · ' || variant.sku
          end,
          'match_decision', line.match_evidence ->> 'decision',
          'clarification_question', line.match_evidence ->> 'clarification_question',
          'show_candidates', coalesce(
            (line.match_evidence ->> 'show_candidates')::boolean,
            false
          ),
          'user_resolution', line.match_evidence ->> 'user_resolution',
          'candidate_choices', case
            when variant.id is not null
              or line.match_evidence ->> 'user_resolution' = 'ignored'
            then '[]'::jsonb
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
    raise exception using
      errcode = 'P0002',
      message = 'Proposal confirmation view was not found';
  end if;
  return v_result;
end;
$$;

create or replace function public.apply_inventory_proposal(
  p_proposal_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_proposal public.transaction_proposals%rowtype;
  v_invalid_line record;
  v_line record;
  v_transaction_id uuid;
  v_transaction_line_id uuid;
  v_transaction_type public.inventory_transaction_type;
  v_quantity_after numeric(24, 8);
begin
  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.id = p_proposal_id
  for update;
  if not found then
    raise exception using
      errcode = 'P0002',
      message = format('Inventory proposal %s was not found', p_proposal_id);
  end if;
  if not exists (
    select 1
    from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_proposal.organization_id
      and member.active
  ) then
    raise exception using
      errcode = '42501',
      message = 'Actor is not an active member of the proposal organization';
  end if;
  if v_proposal.status = 'applied' then
    return v_proposal.applied_transaction_id;
  end if;
  if v_proposal.status <> 'pending_confirmation' then
    raise exception using
      errcode = '22023',
      message = format('Proposal in status %s cannot be applied', v_proposal.status);
  end if;
  if not exists (
    select 1
    from public.proposal_lines as line
    where line.proposal_id = p_proposal_id
      and coalesce(line.match_evidence ->> 'user_resolution', '') <> 'ignored'
  ) then
    raise exception using
      errcode = '22023',
      message = 'A proposal must contain at least one active line';
  end if;

  select
    line.line_number,
    item.tracking_mode,
    line.item_variant_id,
    line.lot_id,
    line.serial_id,
    line.base_quantity_delta,
    line.base_unit
  into v_invalid_line
  from public.proposal_lines as line
  left join public.item_variants as variant
    on variant.organization_id = line.organization_id
   and variant.id = line.item_variant_id
  left join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
  left join public.inventory_lots as lot
    on lot.organization_id = line.organization_id
   and lot.id = line.lot_id
  where line.proposal_id = p_proposal_id
    and coalesce(line.match_evidence ->> 'user_resolution', '') <> 'ignored'
    and (
      line.item_variant_id is null
      or line.base_quantity_delta is null
      or line.base_quantity_delta = 0
      or line.base_unit is null
      or line.match_method is null
      or item.id is null
      or item.base_unit <> line.base_unit
      or (
        item.tracking_mode = 'simple'
        and (line.lot_id is not null or line.serial_id is not null)
      )
      or (
        item.tracking_mode = 'lot'
        and (line.lot_id is null or line.serial_id is not null)
      )
      or (
        item.tracking_mode = 'serial'
        and (line.serial_id is null or line.lot_id is not null)
      )
      or (item.tracking_mode = 'serial' and abs(line.base_quantity_delta) <> 1)
      or (
        item.tracking_mode = 'lot'
        and v_proposal.intent = 'receive_stock'
        and exists (
          select 1
          from public.custom_field_definitions as definition
          where definition.organization_id = line.organization_id
            and definition.entity_type = 'lot'
            and definition.key = 'expiry_date'
            and definition.required_on_receive
            and definition.active
        )
        and lot.expires_on is null
      )
    )
  order by line.line_number
  limit 1;
  if found then
    raise exception using
      errcode = '22023',
      message = format(
        'Proposal line %s is incomplete or violates item tracking rules',
        v_invalid_line.line_number
      );
  end if;

  insert into public.inventory_balances (
    organization_id,
    location_id,
    item_variant_id,
    lot_id,
    serial_id,
    quantity
  )
  select distinct
    v_proposal.organization_id,
    v_proposal.location_id,
    line.item_variant_id,
    line.lot_id,
    line.serial_id,
    0
  from public.proposal_lines as line
  where line.proposal_id = p_proposal_id
    and coalesce(line.match_evidence ->> 'user_resolution', '') <> 'ignored'
  on conflict (organization_id, location_id, item_variant_id, lot_id, serial_id)
  do nothing;

  perform balance.id
  from public.inventory_balances as balance
  join public.proposal_lines as line
    on line.proposal_id = p_proposal_id
   and line.organization_id = balance.organization_id
   and line.item_variant_id = balance.item_variant_id
   and line.lot_id is not distinct from balance.lot_id
   and line.serial_id is not distinct from balance.serial_id
   and coalesce(line.match_evidence ->> 'user_resolution', '') <> 'ignored'
  where balance.location_id = v_proposal.location_id
  order by balance.id
  for update of balance;

  v_transaction_type := case v_proposal.intent
    when 'receive_stock' then 'receive'::public.inventory_transaction_type
    when 'issue_stock' then 'issue'::public.inventory_transaction_type
    when 'adjust_stock' then 'adjustment'::public.inventory_transaction_type
  end;
  insert into public.inventory_transactions (
    organization_id,
    location_id,
    proposal_id,
    transaction_type,
    created_by,
    confirmed_by,
    notes
  )
  values (
    v_proposal.organization_id,
    v_proposal.location_id,
    v_proposal.id,
    v_transaction_type,
    v_proposal.created_by,
    p_actor_id,
    v_proposal.notes
  )
  returning id into v_transaction_id;

  for v_line in
    select
      line.*,
      balance.id as balance_id,
      balance.quantity as quantity_before
    from public.proposal_lines as line
    join public.inventory_balances as balance
      on balance.organization_id = line.organization_id
     and balance.location_id = v_proposal.location_id
     and balance.item_variant_id = line.item_variant_id
     and balance.lot_id is not distinct from line.lot_id
     and balance.serial_id is not distinct from line.serial_id
    where line.proposal_id = p_proposal_id
      and coalesce(line.match_evidence ->> 'user_resolution', '') <> 'ignored'
    order by line.line_number
  loop
    v_quantity_after := v_line.quantity_before + v_line.base_quantity_delta;
    if v_quantity_after < 0 then
      raise exception using
        errcode = 'P0001',
        message = format(
          'Insufficient stock on proposal line %s: current %s, requested delta %s',
          v_line.line_number,
          v_line.quantity_before,
          v_line.base_quantity_delta
        );
    end if;

    insert into public.transaction_lines (
      organization_id,
      transaction_id,
      line_number,
      source_proposal_line_id,
      item_variant_id,
      lot_id,
      serial_id,
      quantity_delta,
      base_unit,
      quantity_before,
      quantity_after,
      source_text,
      match_method,
      match_score,
      match_evidence,
      attributes
    )
    values (
      v_line.organization_id,
      v_transaction_id,
      v_line.line_number,
      v_line.id,
      v_line.item_variant_id,
      v_line.lot_id,
      v_line.serial_id,
      v_line.base_quantity_delta,
      v_line.base_unit,
      v_line.quantity_before,
      v_quantity_after,
      v_line.source_text,
      v_line.match_method,
      v_line.match_score,
      v_line.match_evidence,
      v_line.attributes
    )
    returning id into v_transaction_line_id;

    insert into public.stock_movements (
      organization_id,
      transaction_id,
      transaction_line_id,
      location_id,
      item_variant_id,
      lot_id,
      serial_id,
      quantity_delta
    )
    values (
      v_line.organization_id,
      v_transaction_id,
      v_transaction_line_id,
      v_proposal.location_id,
      v_line.item_variant_id,
      v_line.lot_id,
      v_line.serial_id,
      v_line.base_quantity_delta
    );
    update public.inventory_balances
    set quantity = v_quantity_after,
        version = version + 1,
        updated_at = now()
    where id = v_line.balance_id;
  end loop;

  update public.transaction_proposals
  set status = 'applied',
      confirmed_by = p_actor_id,
      confirmed_at = now(),
      applied_transaction_id = v_transaction_id
  where id = p_proposal_id;
  return v_transaction_id;
end;
$$;

revoke all on function public.mark_proposal_line_as_new_item(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.mark_all_unmatched_proposal_lines_as_new(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.ignore_inventory_proposal_line(uuid, uuid)
  from public, anon, authenticated;

grant execute on function public.mark_proposal_line_as_new_item(uuid, uuid)
  to service_role;
grant execute on function public.mark_all_unmatched_proposal_lines_as_new(uuid, uuid)
  to service_role;
grant execute on function public.ignore_inventory_proposal_line(uuid, uuid)
  to service_role;

comment on function public.mark_proposal_line_as_new_item(uuid, uuid) is
  'Records one auditable add-new decision without starting catalog detail collection.';
comment on function public.mark_all_unmatched_proposal_lines_as_new(uuid, uuid) is
  'Records add-new decisions for every remaining unmatched line in a proposal.';
comment on function public.ignore_inventory_proposal_line(uuid, uuid) is
  'Retains an extracted proposal line for audit while excluding it from stock application.';
