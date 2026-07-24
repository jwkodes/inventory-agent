-- Deterministic development data. Never seed production with this file.

insert into public.organizations (id, name, slug, inventory_profile)
values (
  '10000000-0000-0000-0000-000000000001',
  'Demo SME',
  'demo-sme',
  'general'
)
on conflict (id) do nothing;

insert into public.organization_users (
  id,
  organization_id,
  telegram_user_id,
  display_name,
  role
)
values (
  '11000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  100000001,
  'Demo Manager',
  'manager'
)
on conflict (id) do nothing;

insert into public.locations (id, organization_id, code, name)
values (
  '12000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  'MAIN',
  'Main Warehouse'
)
on conflict (id) do nothing;

insert into public.custom_field_definitions (
  id,
  organization_id,
  entity_type,
  key,
  label,
  data_type,
  required_on_receive,
  searchable,
  validation_rules
)
values
  (
    '13000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    'lot',
    'batch_number',
    'Batch number',
    'text',
    true,
    true,
    '{"matching_role":"operational"}'::jsonb
  ),
  (
    '13000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000001',
    'lot',
    'expiry_date',
    'Expiry date',
    'date',
    true,
    true,
    '{"matching_role":"operational"}'::jsonb
  ),
  (
    '13000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000001',
    'variant',
    'colour',
    'Colour',
    'text',
    false,
    true,
    '{"matching_role":"discriminator"}'::jsonb
  ),
  (
    '13000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000001',
    'variant',
    'size',
    'Size',
    'text',
    false,
    true,
    '{"matching_role":"discriminator"}'::jsonb
  )
on conflict (id) do nothing;

insert into public.items (id, organization_id, name, base_unit, tracking_mode)
values
  (
    '20000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    'Anchor Butter 500g',
    'each',
    'simple'
  ),
  (
    '20000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000001',
    'Full Cream Milk 1L',
    'each',
    'simple'
  ),
  (
    '20000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000001',
    'Amoxicillin 500mg',
    'box',
    'simple'
  ),
  (
    '20000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000001',
    'Classic T-Shirt',
    'each',
    'simple'
  )
on conflict (id) do nothing;

insert into public.item_variants (id, organization_id, item_id, sku, name, attributes)
values
  (
    '21000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000001',
    'BUTTER-ANCHOR-500G',
    null,
    '{}'::jsonb
  ),
  (
    '21000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000002',
    'MILK-FULLCREAM-1L',
    null,
    '{}'::jsonb
  ),
  (
    '21000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000003',
    'MED-AMOX-500',
    null,
    '{"strength":"500mg"}'::jsonb
  ),
  (
    '21000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000004',
    'SHIRT-RED-M',
    'Classic T-Shirt - Red / M',
    '{"colour":"red","size":"M"}'::jsonb
  ),
  (
    '21000000-0000-0000-0000-000000000005',
    '10000000-0000-0000-0000-000000000001',
    '20000000-0000-0000-0000-000000000004',
    'SHIRT-BLUE-L',
    'Classic T-Shirt - Blue / L',
    '{"colour":"blue","size":"L"}'::jsonb
  )
on conflict (id) do nothing;

insert into public.item_identifiers (
  id,
  organization_id,
  item_variant_id,
  identifier_type,
  value,
  normalized_value
)
values
  (
    '23000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000001',
    'sku',
    'BUTTER-ANCHOR-500G',
    'butteranchor500g'
  ),
  (
    '23000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000002',
    'sku',
    'MILK-FULLCREAM-1L',
    'milkfullcream1l'
  ),
  (
    '23000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000003',
    'manufacturer_part_number',
    'AMOX-500',
    'amox500'
  ),
  (
    '23000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000004',
    'sku',
    'SHIRT-RED-M',
    'shirtredm'
  ),
  (
    '23000000-0000-0000-0000-000000000005',
    '10000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000005',
    'sku',
    'SHIRT-BLUE-L',
    'shirtbluel'
  )
on conflict (id) do nothing;

insert into public.item_unit_conversions (
  id,
  organization_id,
  item_variant_id,
  from_unit,
  factor_to_base
)
values (
  '24000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '21000000-0000-0000-0000-000000000001',
  'carton',
  24
)
on conflict (id) do nothing;

insert into public.inventory_lots (
  id,
  organization_id,
  item_variant_id,
  lot_number,
  manufactured_on,
  expires_on
)
values (
  '22000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  '21000000-0000-0000-0000-000000000003',
  'AMX-2607',
  '2026-07-01',
  '2027-06-30'
)
on conflict (id) do nothing;

insert into public.inventory_balances (
  id,
  organization_id,
  location_id,
  item_variant_id,
  lot_id,
  quantity
)
values
  (
    '30000000-0000-0000-0000-000000000001',
    '10000000-0000-0000-0000-000000000001',
    '12000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000001',
    null,
    120
  ),
  (
    '30000000-0000-0000-0000-000000000002',
    '10000000-0000-0000-0000-000000000001',
    '12000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000002',
    null,
    200
  ),
  (
    '30000000-0000-0000-0000-000000000003',
    '10000000-0000-0000-0000-000000000001',
    '12000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000003',
    null,
    50
  ),
  (
    '30000000-0000-0000-0000-000000000004',
    '10000000-0000-0000-0000-000000000001',
    '12000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000004',
    null,
    12
  ),
  (
    '30000000-0000-0000-0000-000000000005',
    '10000000-0000-0000-0000-000000000001',
    '12000000-0000-0000-0000-000000000001',
    '21000000-0000-0000-0000-000000000005',
    null,
    8
  )
on conflict (id) do nothing;
