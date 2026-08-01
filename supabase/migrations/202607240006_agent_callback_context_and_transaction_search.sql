create or replace function public.record_inventory_agent_callback_outcome(
  p_organization_id uuid,
  p_actor_id uuid,
  p_chat_id bigint,
  p_source_event_id uuid,
  p_action text,
  p_result_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_conversation public.inventory_agent_conversations%rowtype;
  v_content text;
  v_history_item jsonb;
begin
  if p_action not in (
    'confirm_proposal',
    'cancel_proposal',
    'confirm_new_item',
    'cancel_new_item',
    'confirm_reversal',
    'cancel_reversal'
  ) then
    return null;
  end if;
  if not exists (
    select 1
    from public.organization_users as member
    where member.organization_id = p_organization_id
      and member.id = p_actor_id
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor is not an active organization member';
  end if;
  if not exists (
    select 1
    from public.source_events as source_event
    where source_event.organization_id = p_organization_id
      and source_event.id = p_source_event_id
      and source_event.event_type = 'callback_query'
      and source_event.status = 'processing'
  ) then
    raise exception using errcode = '22023', message = 'Callback source event is not processing';
  end if;

  select conversation.* into v_conversation
  from public.inventory_agent_conversations as conversation
  where conversation.organization_id = p_organization_id
    and conversation.organization_user_id = p_actor_id
    and conversation.chat_id = p_chat_id
  for update;

  -- Legacy structured flows may have no agent conversation. Their callback still succeeds.
  if not found then
    return null;
  end if;
  if exists (
    select 1
    from public.inventory_agent_turns as turn
    where turn.organization_id = p_organization_id
      and turn.source_event_id = p_source_event_id
  ) then
    return v_conversation.id;
  end if;
  if jsonb_array_length(v_conversation.history) >= 400 then
    raise exception using errcode = '22023', message = 'Agent history requires compaction';
  end if;

  v_content := case p_action
    when 'confirm_proposal' then format(
      'Inventory system event: The user confirmed the stock proposal. Inventory transaction %s was applied successfully. The earlier proposal is no longer pending. Read authoritative transactions before correcting or reversing it.',
      p_result_id
    )
    when 'cancel_proposal' then format(
      'Inventory system event: The user cancelled stock proposal %s. It was not applied and no inventory change resulted from that proposal.',
      p_result_id
    )
    when 'confirm_new_item' then format(
      'Inventory system event: The user confirmed creation of the new catalog item. Stock proposal %s is now ready for review; it is not applied until separately confirmed.',
      p_result_id
    )
    when 'cancel_new_item' then format(
      'Inventory system event: The user cancelled catalog-item creation request %s. No catalog item or inventory transaction was created by that request.',
      p_result_id
    )
    when 'confirm_reversal' then format(
      'Inventory system event: The user confirmed the reversal. Compensating inventory transaction %s was applied successfully. Read authoritative transactions before discussing its current state.',
      p_result_id
    )
    when 'cancel_reversal' then format(
      'Inventory system event: The user cancelled reversal request %s. No reversal transaction was applied.',
      p_result_id
    )
  end;
  v_history_item := jsonb_build_object('role', 'system', 'content', v_content);

  update public.inventory_agent_conversations
  set history = history || jsonb_build_array(v_history_item),
      last_source_event_id = p_source_event_id,
      last_reply_text = v_content,
      last_proposal_id = case
        when p_action = 'cancel_proposal' and last_proposal_id = p_result_id then null
        when p_action = 'confirm_proposal' and exists (
          select 1
          from public.inventory_transactions as transaction
          where transaction.organization_id = p_organization_id
            and transaction.id = p_result_id
            and transaction.proposal_id = last_proposal_id
        ) then null
        else last_proposal_id
      end,
      last_reversal_request_id = case
        when p_action = 'cancel_reversal' and last_reversal_request_id = p_result_id then null
        when p_action = 'confirm_reversal' and exists (
          select 1
          from public.transaction_reversal_requests as request
          where request.organization_id = p_organization_id
            and request.id = last_reversal_request_id
            and request.reversal_transaction_id = p_result_id
        ) then null
        else last_reversal_request_id
      end,
      last_reversal_reason = case
        when p_action in ('confirm_reversal', 'cancel_reversal') then null
        else last_reversal_reason
      end,
      last_response_id = null,
      updated_at = now()
  where id = v_conversation.id;

  insert into public.inventory_agent_turns (
    organization_id,
    conversation_id,
    source_event_id,
    history,
    estimated_tokens,
    input_tokens,
    output_tokens,
    total_tokens,
    created_at
  )
  select
    p_organization_id,
    v_conversation.id,
    p_source_event_id,
    jsonb_build_array(v_history_item),
    greatest(1, ceil(length(v_history_item::text) / 4.0)::integer),
    0,
    0,
    0,
    source_event.received_at
  from public.source_events as source_event
  where source_event.organization_id = p_organization_id
    and source_event.id = p_source_event_id;

  return v_conversation.id;
end;
$$;

revoke all on function public.record_inventory_agent_callback_outcome(
  uuid, uuid, bigint, uuid, text, uuid
) from public, anon, authenticated;
grant execute on function public.record_inventory_agent_callback_outcome(
  uuid, uuid, bigint, uuid, text, uuid
) to service_role;

comment on function public.record_inventory_agent_callback_outcome(
  uuid, uuid, bigint, uuid, text, uuid
) is
  'Adds deterministic callback results to active agent context and immutable turn history.';

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
  ranked as (
    select
      summaries.*,
      count(query_terms.term) filter (
        where lower(concat_ws(' ', summaries.kind, summaries.description))
          like '%' || query_terms.term || '%'
      ) as matched_terms
    from summaries
    left join query_terms on true
    group by
      summaries.id,
      summaries.kind,
      summaries.applied_at,
      summaries.description,
      summaries.was_reversed
  )
  select
    ranked.id::text,
    ranked.kind,
    ranked.applied_at::text,
    ranked.description,
    ranked.was_reversed
  from ranked
  where nullif(trim(p_query), '') is null
    or ranked.matched_terms > 0
  order by
    case when nullif(trim(p_query), '') is not null then ranked.matched_terms end desc,
    ranked.applied_at desc,
    ranked.id desc
  limit least(greatest(coalesce(p_limit, 10), 1), 20);
$$;

comment on function public.read_inventory_agent_transactions(uuid, text, integer) is
  'Returns ranked recent transactions using token-based natural-language matching.';
