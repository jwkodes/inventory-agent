begin;

create extension if not exists pgtap with schema extensions;

select plan(9);

select has_function(
  'public',
  'find_inventory_candidates',
  array['uuid', 'text', 'text', 'text', 'integer'],
  'candidate search function exists'
);

select is(
  public.normalize_inventory_reference(' AMOX-500 '),
  'amox500',
  'inventory references are normalized consistently'
);

select is(
  (
    select candidate.item_variant_id
    from public.find_inventory_candidates(
      '10000000-0000-0000-0000-000000000001',
      'AMOX-500',
      'PART_NUMBER'
    ) as candidate
    limit 1
  ),
  '21000000-0000-0000-0000-000000000003'::uuid,
  'exact manufacturer part number resolves the medicine variant'
);

select is(
  (
    select candidate.match_method::text
    from public.find_inventory_candidates(
      '10000000-0000-0000-0000-000000000001',
      'AMOX-500',
      'PART_NUMBER'
    ) as candidate
    limit 1
  ),
  'exact_identifier',
  'exact part number reports identifier evidence'
);

insert into public.item_aliases (
  organization_id,
  item_variant_id,
  source_text,
  normalized_source_text,
  confirmed_by
)
values (
  '10000000-0000-0000-0000-000000000001',
  '21000000-0000-0000-0000-000000000001',
  'Anchor spread',
  'anchorspread',
  '11000000-0000-0000-0000-000000000001'
);

select is(
  (
    select candidate.item_variant_id
    from public.find_inventory_candidates(
      '10000000-0000-0000-0000-000000000001',
      'Anchor spread',
      'NAME'
    ) as candidate
    limit 1
  ),
  '21000000-0000-0000-0000-000000000001'::uuid,
  'a confirmed alias resolves its variant'
);

select is(
  (
    select candidate.match_method::text
    from public.find_inventory_candidates(
      '10000000-0000-0000-0000-000000000001',
      'Anchor spread',
      'NAME'
    ) as candidate
    limit 1
  ),
  'confirmed_alias',
  'an exact alias outranks fuzzy text candidates'
);

select is(
  (
    select candidate.item_variant_id
    from public.find_inventory_candidates(
      '10000000-0000-0000-0000-000000000001',
      'full creme milk',
      'NAME'
    ) as candidate
    limit 1
  ),
  '21000000-0000-0000-0000-000000000002'::uuid,
  'trigram search tolerates a product-name typo'
);

select is(
  (
    select count(*)
    from public.find_inventory_candidates(
      'ffffffff-ffff-ffff-ffff-ffffffffffff',
      'AMOX-500',
      'PART_NUMBER'
    )
  ),
  0::bigint,
  'candidate search never crosses organization scope'
);

select is(
  (
    select count(*)
    from public.find_inventory_candidates(
      '10000000-0000-0000-0000-000000000001',
      'shirt',
      'NAME',
      null,
      1
    )
  ),
  1::bigint,
  'candidate count respects the requested limit'
);

select * from finish();

rollback;
