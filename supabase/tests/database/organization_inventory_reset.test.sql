begin;

create extension if not exists pgtap with schema extensions;

select plan(12);

select has_table(
  'public',
  'organization_data_resets',
  'inventory resets have a durable audit table'
);
select has_function(
  'public',
  'reset_organization_inventory_data',
  array['uuid', 'uuid', 'text'],
  'organization inventory reset function exists'
);

insert into public.organization_users (
  id,
  organization_id,
  telegram_user_id,
  display_name,
  role
)
values (
  '11000000-0000-0000-0000-000000000099',
  '10000000-0000-0000-0000-000000000001',
  299000099,
  'Reset Test Worker',
  'worker'
);

select throws_ok(
  $$
    select public.reset_organization_inventory_data(
      '10000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001',
      'RESET another-company'
    )
  $$,
  '22023',
  'Type RESET cabybaba-pte-ltd exactly to confirm the inventory data reset',
  'destructive reset requires the exact company-specific phrase'
);
select throws_ok(
  $$
    select public.reset_organization_inventory_data(
      '10000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000099',
      'RESET cabybaba-pte-ltd'
    )
  $$,
  '42501',
  'Only an active organization admin can reset inventory data',
  'workers cannot reset company inventory'
);

create temporary table reset_before as
select
  (select count(*) from public.items
   where organization_id = '10000000-0000-0000-0000-000000000001') as items,
  (select count(*) from public.inventory_transactions
   where organization_id = '10000000-0000-0000-0000-000000000001') as transactions,
  (select count(*) from public.organization_users
   where organization_id = '10000000-0000-0000-0000-000000000001') as members;

select ok(
  (select items > 0 from reset_before),
  'the fixture contains catalog data before reset'
);

create temporary table reset_result as
select public.reset_organization_inventory_data(
  '10000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  'RESET cabybaba-pte-ltd'
) as result;

select is(
  (select result ->> 'status' from reset_result),
  'reset',
  'admin reset completes atomically'
);
select is(
  (
    select
      (select count(*) from public.items
       where organization_id = '10000000-0000-0000-0000-000000000001')
      + (select count(*) from public.item_variants
         where organization_id = '10000000-0000-0000-0000-000000000001')
      + (select count(*) from public.inventory_balances
         where organization_id = '10000000-0000-0000-0000-000000000001')
      + (select count(*) from public.inventory_transactions
         where organization_id = '10000000-0000-0000-0000-000000000001')
      + (select count(*) from public.transaction_proposals
         where organization_id = '10000000-0000-0000-0000-000000000001')
      + (select count(*) from public.source_events
         where organization_id = '10000000-0000-0000-0000-000000000001')
      + (select count(*) from public.inventory_agent_conversations
         where organization_id = '10000000-0000-0000-0000-000000000001')
  ),
  0::bigint,
  'operational inventory, event, and conversation data is empty'
);
select is(
  (
    select count(*) from public.organizations
    where id = '10000000-0000-0000-0000-000000000001'
  ),
  1::bigint,
  'company is preserved'
);
select is(
  (
    select count(*) from public.organization_users
    where organization_id = '10000000-0000-0000-0000-000000000001'
  ),
  (select members from reset_before),
  'approved members and roles are preserved'
);
select ok(
  exists (
    select 1 from public.locations
    where organization_id = '10000000-0000-0000-0000-000000000001'
  ),
  'locations are preserved'
);
select ok(
  exists (
    select 1 from public.custom_field_definitions
    where organization_id = '10000000-0000-0000-0000-000000000001'
  ),
  'custom field configuration is preserved'
);
select is(
  (
    select (reset.deleted_counts ->> 'items')::bigint
    from public.organization_data_resets as reset
    where reset.organization_id = '10000000-0000-0000-0000-000000000001'
    order by reset.created_at desc
    limit 1
  ),
  (select items from reset_before),
  'reset audit records the deleted catalog count'
);

select * from finish();
rollback;
