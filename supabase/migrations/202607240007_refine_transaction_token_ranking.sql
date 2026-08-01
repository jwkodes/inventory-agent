create or replace function public.read_inventory_agent_transactions(
  p_organization_id uuid,
  p_query text default null,
  p_limit integer default 10
)
returns table (
  transaction_id text,
  transaction_type text,
  occurred_at text,
  summary text,
  reversed boolean
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  with summaries as (
    select
      transaction.id,
      transaction.transaction_type::text as kind,
      transaction.applied_at,
      concat(
        initcap(replace(transaction.transaction_type::text, '_', ' ')),
        ': ',
        string_agg(
          concat(
            abs(line.quantity_delta)::text,
            ' ',
            line.base_unit,
            ' ',
            coalesce(variant.name, item.name),
            case when variant.sku is null then '' else ' [' || variant.sku || ']' end
          ),
          ', '
          order by line.line_number
        )
      ) as description,
      exists (
        select 1
        from public.inventory_transactions as reversal
        where reversal.organization_id = transaction.organization_id
          and reversal.reversal_of_transaction_id = transaction.id
          and reversal.status = 'applied'
      ) as was_reversed
    from public.inventory_transactions as transaction
    join public.transaction_lines as line
      on line.organization_id = transaction.organization_id
     and line.transaction_id = transaction.id
    join public.item_variants as variant
      on variant.organization_id = line.organization_id
     and variant.id = line.item_variant_id
    join public.items as item
      on item.organization_id = variant.organization_id
     and item.id = variant.item_id
    where transaction.organization_id = p_organization_id
      and transaction.status = 'applied'
    group by transaction.id
  ),
  raw_terms as (
    select regexp_replace(raw_term, '[^[:alnum:]]', '', 'g') as term
    from regexp_split_to_table(lower(coalesce(p_query, '')), '[^[:alnum:]]+') as raw(raw_term)
  ),
  query_terms as (
    select distinct
      case
        when term in ('sale', 'sold', 'selling', 'deduct', 'deducted', 'deduction', 'issued')
          then 'issue'
        when term in ('delivery', 'delivered', 'receipt', 'received')
          then 'receive'
        when length(term) > 3 and right(term, 1) = 's'
          then left(term, length(term) - 1)
        else term
      end as term
    from raw_terms
    where length(term) >= 2
      and term not in (
        'a', 'an', 'and', 'correct', 'correction', 'find', 'for', 'inventory',
        'need', 'of', 'only', 'or', 'please', 'stock', 'the', 'to',
        'transaction', 'transactions', 'we'
      )
  ),
  query_term_count as (
    select count(*) as total
    from query_terms
  ),
  ranked as (
    select
      summaries.*,
      query_term_count.total as total_terms,
      count(query_terms.term) filter (
        where lower(concat_ws(' ', summaries.kind, summaries.description))
          like '%' || query_terms.term || '%'
      ) as matched_terms
    from summaries
    cross join query_term_count
    left join query_terms on true
    group by
      summaries.id,
      summaries.kind,
      summaries.applied_at,
      summaries.description,
      summaries.was_reversed,
      query_term_count.total
  )
  select
    ranked.id::text,
    ranked.kind,
    ranked.applied_at::text,
    ranked.description,
    ranked.was_reversed
  from ranked
  where nullif(trim(p_query), '') is null
    or (
      ranked.matched_terms > 0
      and (
        ranked.total_terms <= 1
        or ranked.matched_terms >= 2
      )
    )
  order by
    case when nullif(trim(p_query), '') is not null then ranked.matched_terms end desc,
    ranked.applied_at desc,
    ranked.id desc
  limit least(greatest(coalesce(p_limit, 10), 1), 20);
$$;

comment on function public.read_inventory_agent_transactions(uuid, text, integer) is
  'Returns ranked recent transactions using token-based natural-language matching.';
