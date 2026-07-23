create extension if not exists vector with schema extensions;

create table public.inventory_variant_embeddings (
  organization_id uuid not null,
  item_variant_id uuid not null,
  embedding_model text not null,
  embedding_dimensions integer not null,
  content_hash text not null,
  embedding extensions.vector(512) not null,
  updated_at timestamptz not null default now(),
  primary key (organization_id, item_variant_id),
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id) on delete cascade,
  check (embedding_dimensions = 512),
  check (length(trim(embedding_model)) > 0),
  check (length(content_hash) = 64)
);

alter table public.inventory_variant_embeddings enable row level security;
revoke all on table public.inventory_variant_embeddings from public, anon, authenticated;
grant select, insert, update, delete on public.inventory_variant_embeddings to service_role;

create index inventory_variant_embeddings_cosine_idx
  on public.inventory_variant_embeddings
  using hnsw (embedding extensions.vector_cosine_ops);

create or replace function public.inventory_variant_search_text(
  p_item_variant_id uuid
)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select concat_ws(
    ' | ',
    item.name,
    nullif(variant.name, ''),
    variant.sku,
    nullif(item.attributes::text, '{}'),
    nullif(variant.attributes::text, '{}'),
    nullif(aliases.alias_text, '')
  )
  from public.item_variants as variant
  join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
  left join lateral (
    select string_agg(alias.source_text, ' | ' order by alias.source_text) as alias_text
    from public.item_aliases as alias
    where alias.organization_id = variant.organization_id
      and alias.item_variant_id = variant.id
  ) as aliases on true
  where variant.id = p_item_variant_id;
$$;

create or replace function public.list_inventory_embedding_documents(
  p_organization_id uuid
)
returns table (
  item_variant_id uuid,
  search_text text,
  content_hash text,
  stored_content_hash text,
  stored_embedding_model text,
  stored_embedding_dimensions integer
)
language sql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
  select
    variant.id,
    document.search_text,
    encode(extensions.digest(document.search_text, 'sha256'), 'hex'),
    stored.content_hash,
    stored.embedding_model,
    stored.embedding_dimensions
  from public.item_variants as variant
  join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
   and item.active
  cross join lateral (
    select public.inventory_variant_search_text(variant.id) as search_text
  ) as document
  left join public.inventory_variant_embeddings as stored
    on stored.organization_id = variant.organization_id
   and stored.item_variant_id = variant.id
  where variant.organization_id = p_organization_id
    and variant.active
  order by variant.id;
$$;

create or replace function public.upsert_inventory_variant_embeddings(
  p_organization_id uuid,
  p_embedding_model text,
  p_embedding_dimensions integer,
  p_records jsonb
)
returns integer
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_record jsonb;
  v_count integer := 0;
  v_variant_id uuid;
  v_embedding extensions.vector(512);
begin
  if p_embedding_dimensions <> 512 then
    raise exception using errcode = '22023', message = 'Embedding dimensions must be 512';
  end if;
  if jsonb_typeof(p_records) <> 'array' then
    raise exception using errcode = '22023', message = 'Embedding records must be an array';
  end if;

  for v_record in select value from jsonb_array_elements(p_records)
  loop
    v_variant_id := (v_record ->> 'item_variant_id')::uuid;
    if not exists (
      select 1
      from public.item_variants as variant
      where variant.organization_id = p_organization_id
        and variant.id = v_variant_id
        and variant.active
    ) then
      raise exception using errcode = '22023', message = 'Embedding variant is invalid';
    end if;
    v_embedding := (v_record -> 'embedding')::text::extensions.vector(512);
    if extensions.vector_dims(v_embedding) <> p_embedding_dimensions then
      raise exception using errcode = '22023', message = 'Embedding has invalid dimensions';
    end if;

    insert into public.inventory_variant_embeddings (
      organization_id,
      item_variant_id,
      embedding_model,
      embedding_dimensions,
      content_hash,
      embedding
    )
    values (
      p_organization_id,
      v_variant_id,
      p_embedding_model,
      p_embedding_dimensions,
      v_record ->> 'content_hash',
      v_embedding
    )
    on conflict (organization_id, item_variant_id) do update
    set embedding_model = excluded.embedding_model,
        embedding_dimensions = excluded.embedding_dimensions,
        content_hash = excluded.content_hash,
        embedding = excluded.embedding,
        updated_at = now();
    v_count := v_count + 1;
  end loop;
  return v_count;
end;
$$;

create or replace function public.find_semantic_inventory_candidates(
  p_organization_id uuid,
  p_query_embedding text,
  p_embedding_model text,
  p_embedding_dimensions integer default 512,
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
  with query as (
    select
      p_query_embedding::extensions.vector(512) as embedding,
      least(greatest(coalesce(p_limit, 5), 1), 20) as result_limit
    where p_embedding_dimensions = 512
  )
  select
    variant.id,
    item.id,
    item.name,
    variant.name,
    variant.sku,
    item.base_unit,
    item.tracking_mode,
    'semantic_rerank'::public.match_method,
    round(
      greatest(
        0,
        least(1, 1 - (stored.embedding <=> query.embedding))
      )::numeric,
      7
    ),
    jsonb_build_object(
      'source', 'embedding_cosine',
      'embedding_model', stored.embedding_model,
      'embedding_dimensions', stored.embedding_dimensions,
      'content_hash', stored.content_hash
    )
  from public.inventory_variant_embeddings as stored
  join public.item_variants as variant
    on variant.organization_id = stored.organization_id
   and variant.id = stored.item_variant_id
   and variant.active
  join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
   and item.active
  cross join query
  where stored.organization_id = p_organization_id
    and stored.embedding_model = p_embedding_model
    and stored.embedding_dimensions = p_embedding_dimensions
  order by stored.embedding <=> query.embedding, item.name, variant.sku
  limit (select result_limit from query);
$$;

revoke all on function public.inventory_variant_search_text(uuid)
  from public, anon, authenticated;
revoke all on function public.list_inventory_embedding_documents(uuid)
  from public, anon, authenticated;
revoke all on function public.upsert_inventory_variant_embeddings(uuid, text, integer, jsonb)
  from public, anon, authenticated;
revoke all on function public.find_semantic_inventory_candidates(uuid, text, text, integer, integer)
  from public, anon, authenticated;

grant execute on function public.list_inventory_embedding_documents(uuid) to service_role;
grant execute on function public.upsert_inventory_variant_embeddings(
  uuid, text, integer, jsonb
) to service_role;
grant execute on function public.find_semantic_inventory_candidates(
  uuid, text, text, integer, integer
) to service_role;

comment on table public.inventory_variant_embeddings is
  'Cached organization-scoped semantic search vectors for active inventory variants.';
comment on function public.find_semantic_inventory_candidates(
  uuid, text, text, integer, integer
) is 'Returns inventory variants ranked by embedding cosine similarity.';
