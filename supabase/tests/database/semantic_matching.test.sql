begin;

create extension if not exists pgtap with schema extensions;

select plan(8);

select has_table(
  'public',
  'inventory_variant_embeddings',
  'semantic embedding cache exists'
);
select has_function(
  'public',
  'inventory_variant_search_text',
  array['uuid'],
  'variant search document builder exists'
);
select has_function(
  'public',
  'list_inventory_embedding_documents',
  array['uuid'],
  'embedding refresh document lookup exists'
);
select has_function(
  'public',
  'upsert_inventory_variant_embeddings',
  array['uuid', 'text', 'integer', 'jsonb'],
  'embedding cache upsert exists'
);
select has_function(
  'public',
  'find_semantic_inventory_candidates',
  array['uuid', 'text', 'text', 'integer', 'integer'],
  'semantic candidate lookup exists'
);

select ok(
  (
    select count(*) > 0
    from public.list_inventory_embedding_documents(
      '10000000-0000-0000-0000-000000000001'
    )
  ),
  'active variants are exposed for embedding refresh'
);

create temporary table semantic_test_vector as
select jsonb_agg(
  case when position = 1 then 1.0 else 0.0 end
  order by position
) as embedding
from generate_series(1, 512) as position;

select is(
  public.upsert_inventory_variant_embeddings(
    '10000000-0000-0000-0000-000000000001',
    'text-embedding-3-small',
    512,
    jsonb_build_array(
      jsonb_build_object(
        'item_variant_id', '21000000-0000-0000-0000-000000000001',
        'content_hash', (
          select document.content_hash
          from public.list_inventory_embedding_documents(
            '10000000-0000-0000-0000-000000000001'
          ) as document
          where document.item_variant_id = '21000000-0000-0000-0000-000000000001'
        ),
        'embedding', (select embedding from semantic_test_vector)
      )
    )
  ),
  1,
  'one catalog embedding is cached'
);

select is(
  (
    select candidate.item_variant_id
    from public.find_semantic_inventory_candidates(
      '10000000-0000-0000-0000-000000000001',
      (select embedding::text from semantic_test_vector),
      'text-embedding-3-small',
      512,
      5
    ) as candidate
    limit 1
  ),
  '21000000-0000-0000-0000-000000000001'::uuid,
  'cosine search returns the nearest organization-scoped variant'
);

select * from finish();
rollback;
