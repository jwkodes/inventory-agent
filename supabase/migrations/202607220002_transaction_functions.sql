alter table public.inventory_balances
  add constraint inventory_balances_serial_quantity_check
  check (serial_id is null or quantity in (0, 1));

alter table public.transaction_lines
  add constraint transaction_lines_serial_delta_check
  check (serial_id is null or abs(quantity_delta) = 1);

create unique index proposal_lines_resolved_bucket_unique
  on public.proposal_lines (
    organization_id,
    proposal_id,
    item_variant_id,
    lot_id,
    serial_id
  ) nulls not distinct
  where item_variant_id is not null;

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
  select proposal.*
  into v_proposal
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
  ) then
    raise exception using
      errcode = '22023',
      message = 'A proposal must contain at least one line';
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
    and (
      line.item_variant_id is null
      or line.base_quantity_delta is null
      or line.base_quantity_delta = 0
      or line.base_unit is null
      or line.match_method is null
      or item.id is null
      or item.base_unit <> line.base_unit
      or (item.tracking_mode = 'simple' and (line.lot_id is not null or line.serial_id is not null))
      or (item.tracking_mode = 'lot' and (line.lot_id is null or line.serial_id is not null))
      or (item.tracking_mode = 'serial' and (line.serial_id is null or line.lot_id is not null))
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
      message = format('Proposal line %s is incomplete or violates item tracking rules', v_invalid_line.line_number);
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

create or replace function public.reverse_inventory_transaction(
  p_transaction_id uuid,
  p_actor_id uuid,
  p_reason text
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_original public.inventory_transactions%rowtype;
  v_actor_role public.organization_role;
  v_existing_reversal_id uuid;
  v_reversal_id uuid;
  v_reversal_line_id uuid;
  v_line record;
  v_quantity_delta numeric(24, 8);
  v_quantity_after numeric(24, 8);
begin
  select transaction.*
  into v_original
  from public.inventory_transactions as transaction
  where transaction.id = p_transaction_id
  for update;

  if not found then
    raise exception using
      errcode = 'P0002',
      message = format('Inventory transaction %s was not found', p_transaction_id);
  end if;

  select member.role
  into v_actor_role
  from public.organization_users as member
  where member.id = p_actor_id
    and member.organization_id = v_original.organization_id
    and member.active;

  if not found or v_actor_role not in ('manager', 'admin') then
    raise exception using
      errcode = '42501',
      message = 'Only an active manager or admin can reverse a transaction';
  end if;

  if v_original.transaction_type = 'reversal' then
    raise exception using
      errcode = '22023',
      message = 'A reversal transaction cannot itself be reversed';
  end if;

  if p_reason is null or length(trim(p_reason)) = 0 then
    raise exception using
      errcode = '22023',
      message = 'A reversal reason is required';
  end if;

  select transaction.id
  into v_existing_reversal_id
  from public.inventory_transactions as transaction
  where transaction.reversal_of_transaction_id = p_transaction_id;

  if found then
    return v_existing_reversal_id;
  end if;

  perform balance.id
  from public.inventory_balances as balance
  join public.transaction_lines as line
    on line.transaction_id = p_transaction_id
   and line.organization_id = balance.organization_id
   and line.item_variant_id = balance.item_variant_id
   and line.lot_id is not distinct from balance.lot_id
   and line.serial_id is not distinct from balance.serial_id
  where balance.location_id = v_original.location_id
  order by balance.id
  for update of balance;

  insert into public.inventory_transactions (
    organization_id,
    location_id,
    reversal_of_transaction_id,
    transaction_type,
    created_by,
    confirmed_by,
    reason,
    notes
  )
  values (
    v_original.organization_id,
    v_original.location_id,
    v_original.id,
    'reversal',
    p_actor_id,
    p_actor_id,
    trim(p_reason),
    format('Complete reversal of transaction %s', v_original.id)
  )
  returning id into v_reversal_id;

  for v_line in
    select
      line.*,
      balance.id as balance_id,
      balance.quantity as current_quantity
    from public.transaction_lines as line
    join public.inventory_balances as balance
      on balance.organization_id = line.organization_id
     and balance.location_id = v_original.location_id
     and balance.item_variant_id = line.item_variant_id
     and balance.lot_id is not distinct from line.lot_id
     and balance.serial_id is not distinct from line.serial_id
    where line.transaction_id = p_transaction_id
    order by line.line_number
  loop
    v_quantity_delta := -v_line.quantity_delta;
    v_quantity_after := v_line.current_quantity + v_quantity_delta;

    if v_quantity_after < 0 then
      raise exception using
        errcode = 'P0001',
        message = format(
          'Reversal would make stock negative on line %s: current %s, reversal delta %s',
          v_line.line_number,
          v_line.current_quantity,
          v_quantity_delta
        );
    end if;

    insert into public.transaction_lines (
      organization_id,
      transaction_id,
      line_number,
      reversal_of_transaction_line_id,
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
      v_reversal_id,
      v_line.line_number,
      v_line.id,
      v_line.item_variant_id,
      v_line.lot_id,
      v_line.serial_id,
      v_quantity_delta,
      v_line.base_unit,
      v_line.current_quantity,
      v_quantity_after,
      v_line.source_text,
      v_line.match_method,
      v_line.match_score,
      v_line.match_evidence,
      v_line.attributes
    )
    returning id into v_reversal_line_id;

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
      v_reversal_id,
      v_reversal_line_id,
      v_original.location_id,
      v_line.item_variant_id,
      v_line.lot_id,
      v_line.serial_id,
      v_quantity_delta
    );

    update public.inventory_balances
    set quantity = v_quantity_after,
        version = version + 1,
        updated_at = now()
    where id = v_line.balance_id;
  end loop;

  return v_reversal_id;
end;
$$;

revoke all on function public.apply_inventory_proposal(uuid, uuid) from public, anon, authenticated;
revoke all on function public.reverse_inventory_transaction(uuid, uuid, text) from public, anon, authenticated;
grant execute on function public.apply_inventory_proposal(uuid, uuid) to service_role;
grant execute on function public.reverse_inventory_transaction(uuid, uuid, text) to service_role;

comment on function public.apply_inventory_proposal(uuid, uuid) is
  'Atomically applies a confirmed proposal, writes immutable movements, and updates balances.';
comment on function public.reverse_inventory_transaction(uuid, uuid, text) is
  'Creates and applies one complete compensating transaction for an applied transaction.';

create or replace function public.prevent_inventory_ledger_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception using
    errcode = '55000',
    message = format('%s is an immutable inventory ledger table', tg_table_name);
end;
$$;

create trigger inventory_transactions_are_immutable
before update or delete on public.inventory_transactions
for each row execute function public.prevent_inventory_ledger_mutation();

create trigger transaction_lines_are_immutable
before update or delete on public.transaction_lines
for each row execute function public.prevent_inventory_ledger_mutation();

create trigger stock_movements_are_immutable
before update or delete on public.stock_movements
for each row execute function public.prevent_inventory_ledger_mutation();

revoke all on function public.prevent_inventory_ledger_mutation() from public, anon, authenticated;

-- The backend can read balances and ledger rows, but stock changes must go through
-- the security-definer functions above so the balance and audit trail stay atomic.
revoke insert, update, delete on table
  public.inventory_balances,
  public.inventory_transactions,
  public.transaction_lines,
  public.stock_movements
from service_role;
