begin;

create extension if not exists pgtap with schema extensions;

select plan(26);

select has_table(
  'public', 'catalog_item_edit_requests', 'catalog edit audit records exist'
);
select has_column('public', 'items', 'description', 'items can retain a description');
select has_function(
  'public',
  'begin_catalog_item_edit',
  array[
    'uuid', 'uuid', 'uuid', 'bigint', 'text', 'text', 'text', 'text',
    'text[]', 'jsonb', 'jsonb', 'text'
  ],
  'catalog edit review creation exists'
);
select has_function(
  'public', 'confirm_catalog_item_edit', array['uuid', 'uuid'],
  'catalog edit confirmation exists'
);
select has_function(
  'public', 'cancel_catalog_item_edit', array['uuid', 'uuid'],
  'catalog edit cancellation exists'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type, status, payload
)
values (
  '50000000-0000-0000-0000-000000000901',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'catalog-edit-source',
  'message',
  'processing',
  '{}'::jsonb
);

create temporary table catalog_edit_ledger_snapshot as
select
  (select count(*) from public.transaction_lines) as transaction_line_count,
  (select count(*) from public.stock_movements) as movement_count,
  (
    select quantity from public.inventory_balances
    where item_variant_id = '21000000-0000-0000-0000-000000000001'
      and lot_id is null and serial_id is null
  ) as quantity;

create temporary table catalog_edit_request as
select public.begin_catalog_item_edit(
  '21000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000901',
  100000001,
  'Anchor Butter Salted 500g',
  'Salted',
  'BUTTER-SALTED-500G',
  'Salted butter block',
  '{}'::text[],
  '{"brand":"Anchor"}'::jsonb,
  '{"pack":"500g"}'::jsonb,
  'Correct the catalog metadata'
) as request_id;

select is(
  (
    select status::text from public.catalog_item_edit_requests
    where id = (select request_id from catalog_edit_request)
  ),
  'awaiting_confirmation',
  'begin retains a pending review without applying it'
);
select is(
  (
    select before_values ->> 'sku' from public.catalog_item_edit_requests
    where id = (select request_id from catalog_edit_request)
  ),
  'BUTTER-ANCHOR-500G',
  'the audit record retains the previous SKU'
);
select is(
  (
    select after_values ->> 'description' from public.catalog_item_edit_requests
    where id = (select request_id from catalog_edit_request)
  ),
  'Salted butter block',
  'the audit record retains the proposed description'
);
select is(
  (
    select sku from public.item_variants
    where id = '21000000-0000-0000-0000-000000000001'
  ),
  'BUTTER-ANCHOR-500G',
  'begin does not update the catalog'
);
select is(
  public.begin_catalog_item_edit(
    '21000000-0000-0000-0000-000000000001',
    '11000000-0000-0000-0000-000000000001',
    '50000000-0000-0000-0000-000000000901',
    100000001,
    null, null, 'IGNORED-IDEMPOTENT-VALUE', null,
    '{}'::text[], '{}'::jsonb, '{}'::jsonb, 'Retry'
  ),
  (select request_id from catalog_edit_request),
  'source-event retries return the original request'
);
select is(
  public.find_catalog_item_edit_by_source_event(
    '50000000-0000-0000-0000-000000000901'
  ),
  (select request_id from catalog_edit_request),
  'a saved agent turn can recover its edit request'
);
create temporary table catalog_edit_outbox as
select public.enqueue_processing_outcome(
  '10000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000901',
  'catalog_item_edit_confirmation',
  (select request_id from catalog_edit_request),
  100000001,
  '{"agent_reply":"Review the catalog update"}'::jsonb
) as outbox_id;
select is(
  (
    select outcome_type::text from public.processing_outbox
    where id = (select outbox_id from catalog_edit_outbox)
  ),
  'catalog_item_edit_confirmation',
  'catalog edit confirmations can cross the durable outbox boundary'
);
select is(
  (
    select aggregate_id from public.processing_outbox
    where id = (select outbox_id from catalog_edit_outbox)
  ),
  (select request_id from catalog_edit_request),
  'the catalog edit confirmation retains its audited request ID'
);
select is(
  public.confirm_catalog_item_edit(
    (select request_id from catalog_edit_request),
    '11000000-0000-0000-0000-000000000001'
  ),
  (select request_id from catalog_edit_request),
  'confirmation applies the retained review'
);
select is(
  (
    select name from public.items
    where id = '20000000-0000-0000-0000-000000000001'
  ),
  'Anchor Butter Salted 500g',
  'item name is updated'
);
select is(
  (
    select description from public.items
    where id = '20000000-0000-0000-0000-000000000001'
  ),
  'Salted butter block',
  'description is updated'
);
select is(
  (
    select attributes ->> 'brand' from public.items
    where id = '20000000-0000-0000-0000-000000000001'
  ),
  'Anchor',
  'item attributes are patched'
);
select is(
  (
    select sku from public.item_variants
    where id = '21000000-0000-0000-0000-000000000001'
  ),
  'BUTTER-SALTED-500G',
  'SKU is updated on the stable variant'
);
select is(
  (
    select id from public.item_variants
    where sku = 'BUTTER-SALTED-500G'
  ),
  '21000000-0000-0000-0000-000000000001'::uuid,
  'the item variant identity remains unchanged'
);
select is(
  (
    select attributes ->> 'pack' from public.item_variants
    where id = '21000000-0000-0000-0000-000000000001'
  ),
  '500g',
  'variant attributes are patched'
);
select is(
  (select count(*) from public.transaction_lines),
  (select transaction_line_count from catalog_edit_ledger_snapshot),
  'catalog edits do not rewrite transaction lines'
);
select is(
  (select count(*) from public.stock_movements),
  (select movement_count from catalog_edit_ledger_snapshot),
  'catalog edits do not create or rewrite stock movements'
);
select is(
  (
    select quantity from public.inventory_balances
    where item_variant_id = '21000000-0000-0000-0000-000000000001'
      and lot_id is null and serial_id is null
  ),
  (select quantity from catalog_edit_ledger_snapshot),
  'catalog edits do not change stock balances'
);
select is(
  (
    select status::text from public.catalog_item_edit_requests
    where id = (select request_id from catalog_edit_request)
  ),
  'completed',
  'the immutable audit record is marked completed'
);
select is(
  public.confirm_catalog_item_edit(
    (select request_id from catalog_edit_request),
    '11000000-0000-0000-0000-000000000001'
  ),
  (select request_id from catalog_edit_request),
  'confirmation retries are idempotent'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type, status, payload
)
values (
  '50000000-0000-0000-0000-000000000902',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'catalog-edit-duplicate-source',
  'message',
  'processing',
  '{}'::jsonb
);

select throws_ok(
  $$
    select public.begin_catalog_item_edit(
      '21000000-0000-0000-0000-000000000002',
      '11000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000902',
      100000001,
      null, null, 'butter-salted-500g', null,
      '{}'::text[], '{}'::jsonb, '{}'::jsonb, 'Duplicate SKU attempt'
    )
  $$,
  '23505',
  'That SKU is already in use',
  'duplicate SKUs are rejected case-insensitively'
);

select * from finish();
rollback;
