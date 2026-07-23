create or replace function public.normalize_inventory_reference(p_value text)
returns text
language sql
immutable
strict
set search_path = pg_catalog
as $$
  select regexp_replace(lower(trim(p_value)), '[^[:alnum:]]', '', 'g');
$$;

create or replace function public.find_inventory_candidates(
  p_organization_id uuid,
  p_query text,
  p_reference_type text default 'UNKNOWN',
  p_supplier_scope text default null,
  p_limit integer default 5
)
returns table (
  item_variant_id uuid,
  item_id uuid,
  item_name text,
  variant_name text,
  sku text,
  base_unit text,
  tracking_mode public.tracking_mode,
  match_method public.match_method,
  match_score numeric,
  match_evidence jsonb
)
language sql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
  with input as (
    select
      trim(p_query) as raw_query,
      public.normalize_inventory_reference(p_query) as normalized_query,
      upper(coalesce(p_reference_type, 'UNKNOWN')) as reference_type,
      coalesce(trim(p_supplier_scope), '') as supplier_scope,
      least(greatest(coalesce(p_limit, 5), 1), 20) as result_limit
  ),
  identifier_candidates as (
    select
      variant.id as item_variant_id,
      item.id as item_id,
      item.name as item_name,
      variant.name as variant_name,
      variant.sku,
      item.base_unit,
      item.tracking_mode,
      'exact_identifier'::public.match_method as match_method,
      case
        when input.reference_type = 'SKU' and identifier.identifier_type = 'sku' then 1.0
        when input.reference_type = 'BARCODE' and identifier.identifier_type = 'barcode' then 1.0
        when input.reference_type = 'PART_NUMBER'
          and identifier.identifier_type in ('manufacturer_part_number', 'supplier_part_number')
          then 1.0
        else 0.99
      end::numeric as match_score,
      jsonb_build_object(
        'source', 'item_identifier',
        'identifier_type', identifier.identifier_type,
        'matched_value', identifier.value,
        'supplier_scope', nullif(identifier.supplier_scope, '')
      ) as match_evidence
    from public.item_identifiers as identifier
    join public.item_variants as variant
      on variant.organization_id = identifier.organization_id
     and variant.id = identifier.item_variant_id
     and variant.active
    join public.items as item
      on item.organization_id = variant.organization_id
     and item.id = variant.item_id
     and item.active
    cross join input
    where identifier.organization_id = p_organization_id
      and identifier.normalized_value = input.normalized_query
      and (
        identifier.supplier_scope = ''
        or identifier.supplier_scope = input.supplier_scope
      )
  ),
  sku_candidates as (
    select
      variant.id as item_variant_id,
      item.id as item_id,
      item.name as item_name,
      variant.name as variant_name,
      variant.sku,
      item.base_unit,
      item.tracking_mode,
      'exact_identifier'::public.match_method as match_method,
      1.0::numeric as match_score,
      jsonb_build_object(
        'source', 'variant_sku',
        'matched_value', variant.sku
      ) as match_evidence
    from public.item_variants as variant
    join public.items as item
      on item.organization_id = variant.organization_id
     and item.id = variant.item_id
     and item.active
    cross join input
    where variant.organization_id = p_organization_id
      and variant.active
      and public.normalize_inventory_reference(variant.sku) = input.normalized_query
  ),
  alias_candidates as (
    select
      variant.id as item_variant_id,
      item.id as item_id,
      item.name as item_name,
      variant.name as variant_name,
      variant.sku,
      item.base_unit,
      item.tracking_mode,
      'confirmed_alias'::public.match_method as match_method,
      case when alias.supplier_scope = input.supplier_scope and input.supplier_scope <> ''
        then 0.98 else 0.97 end::numeric as match_score,
      jsonb_build_object(
        'source', 'confirmed_alias',
        'matched_value', alias.source_text,
        'supplier_scope', nullif(alias.supplier_scope, '')
      ) as match_evidence
    from public.item_aliases as alias
    join public.item_variants as variant
      on variant.organization_id = alias.organization_id
     and variant.id = alias.item_variant_id
     and variant.active
    join public.items as item
      on item.organization_id = variant.organization_id
     and item.id = variant.item_id
     and item.active
    cross join input
    where alias.organization_id = p_organization_id
      and alias.normalized_source_text = input.normalized_query
      and (
        alias.supplier_scope = ''
        or alias.supplier_scope = input.supplier_scope
      )
  ),
  text_candidates as (
    select
      variant.id as item_variant_id,
      item.id as item_id,
      item.name as item_name,
      variant.name as variant_name,
      variant.sku,
      item.base_unit,
      item.tracking_mode,
      'text_search'::public.match_method as match_method,
      round(
        greatest(
          extensions.similarity(lower(item.name), lower(input.raw_query)),
          extensions.similarity(lower(coalesce(variant.name, '')), lower(input.raw_query)),
          extensions.similarity(
            public.normalize_inventory_reference(variant.sku),
            input.normalized_query
          )
        )::numeric * 0.85,
        7
      ) as match_score,
      jsonb_build_object(
        'source', 'trigram',
        'item_name_similarity', extensions.similarity(lower(item.name), lower(input.raw_query)),
        'variant_name_similarity', extensions.similarity(
          lower(coalesce(variant.name, '')),
          lower(input.raw_query)
        ),
        'sku_similarity', extensions.similarity(
          public.normalize_inventory_reference(variant.sku),
          input.normalized_query
        )
      ) as match_evidence
    from public.item_variants as variant
    join public.items as item
      on item.organization_id = variant.organization_id
     and item.id = variant.item_id
     and item.active
    cross join input
    where variant.organization_id = p_organization_id
      and variant.active
  ),
  alias_text_candidates as (
    select
      variant.id as item_variant_id,
      item.id as item_id,
      item.name as item_name,
      variant.name as variant_name,
      variant.sku,
      item.base_unit,
      item.tracking_mode,
      'text_search'::public.match_method as match_method,
      round(
        extensions.similarity(lower(alias.source_text), lower(input.raw_query))::numeric * 0.88,
        7
      ) as match_score,
      jsonb_build_object(
        'source', 'alias_trigram',
        'matched_value', alias.source_text,
        'supplier_scope', nullif(alias.supplier_scope, '')
      ) as match_evidence
    from public.item_aliases as alias
    join public.item_variants as variant
      on variant.organization_id = alias.organization_id
     and variant.id = alias.item_variant_id
     and variant.active
    join public.items as item
      on item.organization_id = variant.organization_id
     and item.id = variant.item_id
     and item.active
    cross join input
    where alias.organization_id = p_organization_id
      and (
        alias.supplier_scope = ''
        or alias.supplier_scope = input.supplier_scope
      )
  ),
  combined as (
    select * from identifier_candidates
    union all
    select * from sku_candidates
    union all
    select * from alias_candidates
    union all
    select * from text_candidates
    union all
    select * from alias_text_candidates
  ),
  ranked as (
    select
      combined.*,
      row_number() over (
        partition by combined.item_variant_id
        order by combined.match_score desc, combined.match_method
      ) as variant_rank
    from combined
    where combined.match_score >= 0.15
  )
  select
    ranked.item_variant_id,
    ranked.item_id,
    ranked.item_name,
    ranked.variant_name,
    ranked.sku,
    ranked.base_unit,
    ranked.tracking_mode,
    ranked.match_method,
    ranked.match_score,
    ranked.match_evidence
  from ranked
  cross join input
  where ranked.variant_rank = 1
  order by ranked.match_score desc, ranked.item_name, ranked.sku
  limit (select result_limit from input);
$$;

revoke all on function public.normalize_inventory_reference(text) from public, anon, authenticated;
revoke all on function public.find_inventory_candidates(uuid, text, text, text, integer)
  from public, anon, authenticated;
grant execute on function public.find_inventory_candidates(uuid, text, text, text, integer)
  to service_role;

comment on function public.find_inventory_candidates(uuid, text, text, text, integer) is
  'Returns organization-scoped exact identifier, confirmed alias, and trigram candidates.';
