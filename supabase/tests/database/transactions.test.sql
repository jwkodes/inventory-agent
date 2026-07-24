begin;

create extension if not exists pgtap with schema extensions;

select plan(30);

select has_table('public', 'organizations', 'organizations table exists');
select has_table('public', 'item_variants', 'item variants table exists');
select has_table('public', 'inventory_lots', 'inventory lots table exists');
select has_table('public', 'inventory_balances', 'inventory balances table exists');
select has_table('public', 'inventory_transactions', 'inventory transactions table exists');
select has_table('public', 'stock_movements', 'immutable stock movements table exists');

select has_function(
  'public',
  'apply_inventory_proposal',
  array['uuid', 'uuid'],
  'atomic proposal application function exists'
);
select has_function(
  'public',
  'reverse_inventory_transaction',
  array['uuid', 'uuid', 'text'],
  'complete reversal function exists'
);

select ok(
  (
    select class.relrowsecurity
    from pg_class as class
    join pg_namespace as namespace on namespace.oid = class.relnamespace
    where namespace.nspname = 'public'
      and class.relname = 'inventory_balances'
  ),
  'row level security is enabled on inventory balances'
);

insert into public.transaction_proposals (
  id,
  organization_id,
  location_id,
  created_by,
  intent,
  idempotency_key,
  raw_command
)
values (
  '40000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'test-receive-milk-3',
  '{"intent":"RECEIVE_STOCK"}'::jsonb
);

insert into public.proposal_lines (
  id,
  organization_id,
  proposal_id,
  line_number,
  source_text,
  extracted_description,
  requested_quantity,
  requested_unit,
  item_variant_id,
  base_quantity_delta,
  base_unit,
  match_method,
  match_score
)
values (
  '41000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000001',
  1,
  'received 3 units of milk',
  'milk',
  3,
  'each',
  '21000000-0000-0000-0000-000000000002',
  3,
  'each',
  'exact_identifier',
  1
);

select lives_ok(
  $$
    select public.apply_inventory_proposal(
      '40000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001'
    )
  $$,
  'a valid receipt proposal applies atomically'
);

select is(
  (
    select balance.quantity
    from public.inventory_balances as balance
    where balance.item_variant_id = '21000000-0000-0000-0000-000000000002'
      and balance.lot_id is null
      and balance.serial_id is null
  ),
  203::numeric,
  'applying the receipt updates the materialized balance'
);
select is(
  (
    select proposal.status::text
    from public.transaction_proposals as proposal
    where proposal.id = '40000000-0000-0000-0000-000000000001'
  ),
  'applied',
  'the proposal is marked applied'
);
select is(
  (
    select count(*)
    from public.inventory_transactions as transaction
    where transaction.proposal_id = '40000000-0000-0000-0000-000000000001'
  ),
  1::bigint,
  'one inventory transaction is created'
);
select is(
  (
    select count(*)
    from public.stock_movements as movement
    join public.inventory_transactions as transaction
      on transaction.id = movement.transaction_id
    where transaction.proposal_id = '40000000-0000-0000-0000-000000000001'
  ),
  1::bigint,
  'one immutable movement is created'
);

select lives_ok(
  $$
    select public.apply_inventory_proposal(
      '40000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001'
    )
  $$,
  'reapplying the same proposal is idempotent'
);
select is(
  (
    select balance.quantity
    from public.inventory_balances as balance
    where balance.item_variant_id = '21000000-0000-0000-0000-000000000002'
      and balance.lot_id is null
      and balance.serial_id is null
  ),
  203::numeric,
  'idempotent reapplication does not change stock twice'
);

select lives_ok(
  $$
    select public.reverse_inventory_transaction(
      (
        select proposal.applied_transaction_id
        from public.transaction_proposals as proposal
        where proposal.id = '40000000-0000-0000-0000-000000000001'
      ),
      '11000000-0000-0000-0000-000000000001',
      'Database test reversal'
    )
  $$,
  'an active manager can reverse an applied transaction'
);
select is(
  (
    select balance.quantity
    from public.inventory_balances as balance
    where balance.item_variant_id = '21000000-0000-0000-0000-000000000002'
      and balance.lot_id is null
      and balance.serial_id is null
  ),
  200::numeric,
  'the compensating transaction restores the net quantity'
);
select is(
  (
    select count(*)
    from public.inventory_transactions as reversal
    join public.transaction_proposals as proposal
      on proposal.applied_transaction_id = reversal.reversal_of_transaction_id
    where proposal.id = '40000000-0000-0000-0000-000000000001'
      and reversal.transaction_type = 'reversal'
  ),
  1::bigint,
  'one linked reversal transaction is retained'
);
select is(
  (
    select sum(movement.quantity_delta)
    from public.stock_movements as movement
    join public.inventory_transactions as transaction
      on transaction.id = movement.transaction_id
    left join public.inventory_transactions as reversal
      on reversal.id = transaction.id
    where transaction.proposal_id = '40000000-0000-0000-0000-000000000001'
       or transaction.reversal_of_transaction_id = (
         select proposal.applied_transaction_id
         from public.transaction_proposals as proposal
         where proposal.id = '40000000-0000-0000-0000-000000000001'
       )
  ),
  0::numeric,
  'original and reversal movements sum to zero'
);

select lives_ok(
  $$
    select public.reverse_inventory_transaction(
      (
        select proposal.applied_transaction_id
        from public.transaction_proposals as proposal
        where proposal.id = '40000000-0000-0000-0000-000000000001'
      ),
      '11000000-0000-0000-0000-000000000001',
      'Repeated database test reversal'
    )
  $$,
  'repeating a complete reversal is idempotent'
);
select is(
  (
    select count(*)
    from public.inventory_transactions as reversal
    join public.transaction_proposals as proposal
      on proposal.applied_transaction_id = reversal.reversal_of_transaction_id
    where proposal.id = '40000000-0000-0000-0000-000000000001'
  ),
  1::bigint,
  'idempotent reversal does not create another transaction'
);

select throws_like(
  $$
    update public.inventory_transactions
    set notes = 'attempted mutation'
    where proposal_id = '40000000-0000-0000-0000-000000000001'
  $$,
  '%immutable inventory ledger table%',
  'applied transaction headers are immutable'
);
select throws_like(
  $$
    delete from public.transaction_lines
    where transaction_id = (
      select applied_transaction_id
      from public.transaction_proposals
      where id = '40000000-0000-0000-0000-000000000001'
    )
  $$,
  '%immutable inventory ledger table%',
  'applied transaction lines are immutable'
);
select throws_like(
  $$
    delete from public.stock_movements
    where transaction_id = (
      select applied_transaction_id
      from public.transaction_proposals
      where id = '40000000-0000-0000-0000-000000000001'
    )
  $$,
  '%immutable inventory ledger table%',
  'stock movements are immutable'
);

insert into public.transaction_proposals (
  id,
  organization_id,
  location_id,
  created_by,
  intent,
  idempotency_key
)
values (
  '40000000-0000-0000-0000-000000000002',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  'issue_stock',
  'test-over-issue-milk'
);

insert into public.proposal_lines (
  id,
  organization_id,
  proposal_id,
  line_number,
  source_text,
  requested_quantity,
  requested_unit,
  item_variant_id,
  base_quantity_delta,
  base_unit,
  match_method,
  match_score
)
values (
  '41000000-0000-0000-0000-000000000002',
  '10000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000002',
  1,
  'issue 201 units of milk',
  201,
  'each',
  '21000000-0000-0000-0000-000000000002',
  -201,
  'each',
  'exact_identifier',
  1
);

select throws_like(
  $$
    select public.apply_inventory_proposal(
      '40000000-0000-0000-0000-000000000002',
      '11000000-0000-0000-0000-000000000001'
    )
  $$,
  '%Insufficient stock%',
  'a transaction that would make stock negative is rejected'
);
select is(
  (
    select balance.quantity
    from public.inventory_balances as balance
    where balance.item_variant_id = '21000000-0000-0000-0000-000000000002'
      and balance.lot_id is null
      and balance.serial_id is null
  ),
  200::numeric,
  'a failed application rolls back its balance update'
);
select is(
  (
    select count(*)
    from public.inventory_transactions as transaction
    where transaction.proposal_id = '40000000-0000-0000-0000-000000000002'
  ),
  0::bigint,
  'a failed application leaves no partial inventory transaction'
);

update public.items
set tracking_mode = 'lot'
where id = '20000000-0000-0000-0000-000000000003';

insert into public.transaction_proposals (
  id,
  organization_id,
  location_id,
  created_by,
  intent,
  idempotency_key
)
values (
  '40000000-0000-0000-0000-000000000003',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'test-receive-amoxicillin-lot'
);

insert into public.proposal_lines (
  id,
  organization_id,
  proposal_id,
  line_number,
  source_text,
  requested_quantity,
  requested_unit,
  item_variant_id,
  lot_id,
  base_quantity_delta,
  base_unit,
  match_method,
  match_score
)
values (
  '41000000-0000-0000-0000-000000000003',
  '10000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000003',
  1,
  'received 5 boxes amoxicillin batch AMX-2607',
  5,
  'box',
  '21000000-0000-0000-0000-000000000003',
  '22000000-0000-0000-0000-000000000001',
  5,
  'box',
  'exact_identifier',
  1
);

select lives_ok(
  $$
    select public.apply_inventory_proposal(
      '40000000-0000-0000-0000-000000000003',
      '11000000-0000-0000-0000-000000000001'
    )
  $$,
  'a lot-tracked receipt with expiry applies'
);
select is(
  (
    select balance.quantity
    from public.inventory_balances as balance
    where balance.item_variant_id = '21000000-0000-0000-0000-000000000003'
      and balance.lot_id = '22000000-0000-0000-0000-000000000001'
  ),
  5::numeric,
  'lot-tracked inventory updates the specific lot balance'
);

select * from finish();
rollback;
