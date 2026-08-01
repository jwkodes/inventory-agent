begin;

create extension if not exists pgtap with schema extensions;

select plan(8);

select ok(
  not (
    select attribute.attnotnull
    from pg_attribute as attribute
    where attribute.attrelid = 'public.item_variants'::regclass
      and attribute.attname = 'sku'
  ),
  'catalog variants may defer their SKU'
);
select has_function(
  'public', 'defer_catalog_item_creation_sku', array['uuid', 'uuid', 'uuid'],
  'single-item SKU deferral is explicit'
);
select has_function(
  'public', 'defer_catalog_batch_skus', array['uuid', 'uuid', 'uuid'],
  'batch SKU deferral is explicit'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type, status, payload
)
values (
  '50000000-0000-0000-0000-000000000904',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'optional-sku-proposal-source',
  'message',
  'processed',
  '{}'::jsonb
);

insert into public.transaction_proposals (
  id, organization_id, location_id, source_event_id, created_by, intent,
  idempotency_key, raw_command
)
values (
  '40000000-0000-0000-0000-000000000904',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000904',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'optional-sku-proposal',
  '{"lines":[{"item_reference":{"type":"NAME","value":"MacBook Air M5"}}]}'::jsonb
);

insert into public.proposal_lines (
  id, organization_id, proposal_id, line_number, source_text,
  extracted_description, requested_quantity, requested_unit, match_evidence, attributes
)
values (
  '41000000-0000-0000-0000-000000000904',
  '10000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000904',
  1,
  '10 MacBook Air M5',
  'MacBook Air M5',
  10,
  'each',
  '{
    "decision":"not_found",
    "new_item":{
      "name":"MacBook Air M5",
      "sku":null,
      "sku_deferred":true,
      "base_unit":"each",
      "tracking_mode":"simple",
      "attributes":[]
    },
    "candidates":[]
  }'::jsonb,
  '{}'::jsonb
);

create temporary table optional_sku_result as
select public.create_catalog_item_from_agent_preview(
  '41000000-0000-0000-0000-000000000904',
  '11000000-0000-0000-0000-000000000001',
  100000001
) as result;

select is(
  (select result ->> 'status' from optional_sku_result),
  'completed',
  'an explicitly SKU-less agent preview creates the catalog product'
);
select is(
  (
    select request.sku_deferred
    from public.catalog_item_creation_requests as request
    where request.proposal_line_id = '41000000-0000-0000-0000-000000000904'
  ),
  true,
  'the audit state records that SKU assignment was deferred'
);
select is(
  (
    select variant.sku
    from public.catalog_item_creation_requests as request
    join public.item_variants as variant on variant.id = request.created_variant_id
    where request.proposal_line_id = '41000000-0000-0000-0000-000000000904'
  ),
  null,
  'the created variant stores a real null SKU'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type, status, payload
)
values (
  '50000000-0000-0000-0000-000000000905',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'optional-sku-later-edit-source',
  'message',
  'processing',
  '{}'::jsonb
);

create temporary table optional_sku_edit as
select public.begin_catalog_item_edit(
  (
    select request.created_variant_id
    from public.catalog_item_creation_requests as request
    where request.proposal_line_id = '41000000-0000-0000-0000-000000000904'
  ),
  '11000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000905',
  100000001,
  null, null, 'MAC-AIR-M5', null,
  '{}'::text[], '{}'::jsonb, '{}'::jsonb,
  'Assign the SKU now'
) as request_id;

select is(
  public.confirm_catalog_item_edit(
    (select request_id from optional_sku_edit),
    '11000000-0000-0000-0000-000000000001'
  ),
  (select request_id from optional_sku_edit),
  'the catalog edit flow can assign the deferred SKU later'
);
select is(
  (
    select variant.sku
    from public.catalog_item_creation_requests as request
    join public.item_variants as variant on variant.id = request.created_variant_id
    where request.proposal_line_id = '41000000-0000-0000-0000-000000000904'
  ),
  'MAC-AIR-M5',
  'the same catalog variant now has its assigned SKU'
);

select * from finish();
rollback;
