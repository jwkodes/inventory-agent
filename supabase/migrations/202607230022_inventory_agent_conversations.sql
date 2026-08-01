alter type public.processing_outcome_type
  add value if not exists 'agent_message';

create table public.inventory_agent_conversations (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  organization_user_id uuid not null,
  chat_id bigint not null,
  history jsonb not null default '[]'::jsonb,
  allowed_variant_ids uuid[] not null default '{}'::uuid[],
  allowed_transaction_ids uuid[] not null default '{}'::uuid[],
  last_source_event_id uuid,
  last_reply_text text,
  last_proposal_id uuid,
  last_reversal_request_id uuid,
  last_reversal_reason text,
  last_response_id text,
  model_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (organization_id, organization_user_id)
    references public.organization_users (organization_id, id) on delete cascade,
  foreign key (organization_id, last_source_event_id)
    references public.source_events (organization_id, id),
  foreign key (organization_id, last_proposal_id)
    references public.transaction_proposals (organization_id, id),
  foreign key (last_reversal_request_id)
    references public.transaction_reversal_requests (id),
  unique (organization_id, organization_user_id, chat_id),
  unique (organization_id, id),
  check (jsonb_typeof(history) = 'array'),
  check (jsonb_array_length(history) <= 400),
  check (last_reply_text is null or length(last_reply_text) between 1 and 8000),
  check (last_reversal_reason is null or length(last_reversal_reason) between 1 and 1000),
  check (not (last_proposal_id is not null and last_reversal_request_id is not null))
);

alter table public.inventory_agent_conversations enable row level security;
grant select, insert, update, delete on public.inventory_agent_conversations to service_role;

create or replace function public.load_inventory_agent_conversation(
  p_organization_id uuid,
  p_actor_id uuid,
  p_chat_id bigint
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_conversation public.inventory_agent_conversations%rowtype;
begin
  if not exists (
    select 1
    from public.organization_users as member
    where member.organization_id = p_organization_id
      and member.id = p_actor_id
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor is not an active organization member';
  end if;

  insert into public.inventory_agent_conversations (
    organization_id,
    organization_user_id,
    chat_id
  )
  values (p_organization_id, p_actor_id, p_chat_id)
  on conflict (organization_id, organization_user_id, chat_id) do nothing;

  select conversation.* into v_conversation
  from public.inventory_agent_conversations as conversation
  where conversation.organization_id = p_organization_id
    and conversation.organization_user_id = p_actor_id
    and conversation.chat_id = p_chat_id;

  return jsonb_build_object(
    'conversation_id', v_conversation.id,
    'organization_id', v_conversation.organization_id,
    'organization_user_id', v_conversation.organization_user_id,
    'chat_id', v_conversation.chat_id,
    'history', v_conversation.history,
    'allowed_variant_ids', to_jsonb(v_conversation.allowed_variant_ids),
    'allowed_transaction_ids', to_jsonb(v_conversation.allowed_transaction_ids),
    'last_source_event_id', v_conversation.last_source_event_id,
    'last_reply_text', v_conversation.last_reply_text,
    'last_proposal_id', v_conversation.last_proposal_id,
    'last_reversal_request_id', v_conversation.last_reversal_request_id,
    'last_reversal_reason', v_conversation.last_reversal_reason,
    'last_response_id', v_conversation.last_response_id,
    'model_name', v_conversation.model_name
  );
end;
$$;

create or replace function public.save_inventory_agent_conversation(
  p_conversation_id uuid,
  p_source_event_id uuid,
  p_actor_id uuid,
  p_history jsonb,
  p_allowed_variant_ids uuid[],
  p_allowed_transaction_ids uuid[],
  p_reply_text text,
  p_proposal_id uuid,
  p_reversal_request_id uuid,
  p_reversal_reason text,
  p_response_id text,
  p_model_name text
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_conversation public.inventory_agent_conversations%rowtype;
begin
  select conversation.* into v_conversation
  from public.inventory_agent_conversations as conversation
  where conversation.id = p_conversation_id
  for update;

  if not found then
    raise exception using errcode = 'P0002', message = 'Agent conversation was not found';
  end if;
  if v_conversation.organization_user_id <> p_actor_id or not exists (
    select 1
    from public.organization_users as member
    where member.organization_id = v_conversation.organization_id
      and member.id = p_actor_id
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot update this conversation';
  end if;
  if jsonb_typeof(p_history) <> 'array' or jsonb_array_length(p_history) > 400 then
    raise exception using errcode = '22023', message = 'Agent history must be an array of at most 400 items';
  end if;
  if nullif(trim(p_reply_text), '') is null then
    raise exception using errcode = '22023', message = 'Agent reply text is required';
  end if;
  if p_proposal_id is not null and p_reversal_request_id is not null then
    raise exception using errcode = '22023', message = 'A turn cannot propose stock and reversal together';
  end if;
  if not exists (
    select 1
    from public.source_events as source_event
    where source_event.organization_id = v_conversation.organization_id
      and source_event.id = p_source_event_id
      and source_event.status = 'processing'
  ) then
    raise exception using errcode = '22023', message = 'Source event is not processing in this organization';
  end if;
  if p_proposal_id is not null and not exists (
    select 1
    from public.transaction_proposals as proposal
    where proposal.organization_id = v_conversation.organization_id
      and proposal.id = p_proposal_id
      and proposal.source_event_id = p_source_event_id
  ) then
    raise exception using errcode = '22023', message = 'Proposal does not belong to this conversation turn';
  end if;
  if p_reversal_request_id is not null and not exists (
    select 1
    from public.transaction_reversal_requests as request
    where request.organization_id = v_conversation.organization_id
      and request.id = p_reversal_request_id
      and request.reason_source_event_id = p_source_event_id
      and request.reason = nullif(trim(p_reversal_reason), '')
  ) then
    raise exception using errcode = '22023', message = 'Reversal does not belong to this conversation turn';
  end if;
  if exists (
    select 1
    from unnest(coalesce(p_allowed_variant_ids, '{}'::uuid[])) as requested(variant_id)
    where not exists (
      select 1
      from public.item_variants as variant
      where variant.organization_id = v_conversation.organization_id
        and variant.id = requested.variant_id
    )
  ) then
    raise exception using errcode = '22023', message = 'Conversation contains a cross-organization variant';
  end if;
  if exists (
    select 1
    from unnest(coalesce(p_allowed_transaction_ids, '{}'::uuid[])) as requested(transaction_id)
    where not exists (
      select 1
      from public.inventory_transactions as transaction
      where transaction.organization_id = v_conversation.organization_id
        and transaction.id = requested.transaction_id
    )
  ) then
    raise exception using errcode = '22023', message = 'Conversation contains a cross-organization transaction';
  end if;

  update public.inventory_agent_conversations
  set history = p_history,
      allowed_variant_ids = coalesce(p_allowed_variant_ids, '{}'::uuid[]),
      allowed_transaction_ids = coalesce(p_allowed_transaction_ids, '{}'::uuid[]),
      last_source_event_id = p_source_event_id,
      last_reply_text = trim(p_reply_text),
      last_proposal_id = p_proposal_id,
      last_reversal_request_id = p_reversal_request_id,
      last_reversal_reason = nullif(trim(p_reversal_reason), ''),
      last_response_id = nullif(trim(p_response_id), ''),
      model_name = nullif(trim(p_model_name), ''),
      updated_at = now()
  where id = v_conversation.id;

  return v_conversation.id;
end;
$$;

create or replace function public.get_inventory_agent_variant_balances(
  p_organization_id uuid,
  p_location_id uuid,
  p_variant_ids uuid[]
)
returns table (
  item_variant_id uuid,
  on_hand numeric
)
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
begin
  if not exists (
    select 1
    from public.locations as location
    where location.organization_id = p_organization_id
      and location.id = p_location_id
      and location.active
  ) then
    raise exception using errcode = '22023', message = 'Location is not active in the organization';
  end if;

  return query
  select
    variant.id,
    coalesce(sum(balance.quantity), 0)::numeric
  from unnest(coalesce(p_variant_ids, '{}'::uuid[])) as requested(variant_id)
  join public.item_variants as variant
    on variant.organization_id = p_organization_id
   and variant.id = requested.variant_id
   and variant.active
  left join public.inventory_balances as balance
    on balance.organization_id = variant.organization_id
   and balance.location_id = p_location_id
   and balance.item_variant_id = variant.id
  group by variant.id;
end;
$$;

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
  )
  select
    summaries.id::text,
    summaries.kind,
    summaries.applied_at::text,
    summaries.description,
    summaries.was_reversed
  from summaries
  where nullif(trim(p_query), '') is null
    or summaries.id::text ilike '%' || trim(p_query) || '%'
    or summaries.kind ilike '%' || trim(p_query) || '%'
    or summaries.description ilike '%' || trim(p_query) || '%'
  order by summaries.applied_at desc, summaries.id desc
  limit least(greatest(coalesce(p_limit, 10), 1), 20);
$$;

alter table public.processing_outbox
  drop constraint processing_outbox_aggregate_check,
  add constraint processing_outbox_aggregate_check check (
    (
      outcome_type in (
        'proposal_ready',
        'transaction_applied',
        'catalog_item_details_required',
        'catalog_item_confirmation',
        'reversal_reason_required',
        'reversal_confirmation'
      )
      and aggregate_id is not null
    )
    or (
      outcome_type not in (
        'proposal_ready',
        'transaction_applied',
        'catalog_item_details_required',
        'catalog_item_confirmation',
        'reversal_reason_required',
        'reversal_confirmation'
      )
      and aggregate_id is null
    )
  );

create or replace function public.enqueue_processing_outcome(
  p_organization_id uuid,
  p_source_event_id uuid,
  p_outcome_type public.processing_outcome_type,
  p_aggregate_id uuid,
  p_chat_id bigint,
  p_payload jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_outbox_id uuid;
begin
  if not exists (
    select 1 from public.source_events as source_event
    where source_event.organization_id = p_organization_id
      and source_event.id = p_source_event_id
  ) then
    raise exception using errcode = '22023', message = 'Source event is not in the organization';
  end if;
  if p_outcome_type = 'proposal_ready' and not exists (
    select 1 from public.transaction_proposals as proposal
    where proposal.organization_id = p_organization_id and proposal.id = p_aggregate_id
  ) then
    raise exception using errcode = '22023', message = 'Proposal is not in the organization';
  end if;
  if p_outcome_type = 'transaction_applied' and not exists (
    select 1 from public.inventory_transactions as transaction
    where transaction.organization_id = p_organization_id
      and transaction.id = p_aggregate_id
      and transaction.status = 'applied'
  ) then
    raise exception using errcode = '22023', message = 'Applied transaction is not in the organization';
  end if;
  if p_outcome_type in ('catalog_item_details_required', 'catalog_item_confirmation')
    and not exists (
      select 1 from public.catalog_item_creation_requests as request
      where request.organization_id = p_organization_id
        and request.id = p_aggregate_id
        and request.chat_id = p_chat_id
        and (
          (p_outcome_type = 'catalog_item_details_required'
            and request.status = 'awaiting_details')
          or
          (p_outcome_type = 'catalog_item_confirmation'
            and request.status = 'awaiting_confirmation')
        )
    )
  then
    raise exception using errcode = '22023', message = 'Catalog request state does not match outcome';
  end if;
  if p_outcome_type in ('callback_notice', 'agent_message')
    and nullif(trim(p_payload ->> 'message'), '') is null
  then
    raise exception using errcode = '22023', message = 'Text outcome requires a message';
  end if;
  if p_outcome_type = 'reversal_reason_required' and not exists (
    select 1 from public.transaction_reversal_requests as request
    where request.organization_id = p_organization_id
      and request.id = p_aggregate_id
      and request.status = 'awaiting_reason'
      and request.chat_id = p_chat_id
  ) then
    raise exception using errcode = '22023', message = 'Pending reversal reason request does not match organization or chat';
  end if;
  if p_outcome_type = 'reversal_confirmation' and not exists (
    select 1 from public.transaction_reversal_requests as request
    where request.organization_id = p_organization_id
      and request.id = p_aggregate_id
      and request.status = 'awaiting_confirmation'
      and request.chat_id = p_chat_id
      and request.reason = nullif(trim(p_payload ->> 'reason'), '')
  ) then
    raise exception using errcode = '22023', message = 'Pending reversal confirmation does not match organization, chat, or reason';
  end if;

  insert into public.processing_outbox (
    organization_id, source_event_id, outcome_type, aggregate_id, chat_id, payload
  )
  values (
    p_organization_id, p_source_event_id, p_outcome_type,
    p_aggregate_id, p_chat_id, coalesce(p_payload, '{}'::jsonb)
  )
  on conflict (source_event_id) do nothing
  returning id into v_outbox_id;
  if v_outbox_id is null then
    select outbox.id into v_outbox_id
    from public.processing_outbox as outbox
    where outbox.source_event_id = p_source_event_id;
  end if;
  return v_outbox_id;
end;
$$;

revoke all on function public.load_inventory_agent_conversation(uuid, uuid, bigint)
  from public, anon, authenticated;
revoke all on function public.save_inventory_agent_conversation(
  uuid, uuid, uuid, jsonb, uuid[], uuid[], text, uuid, uuid, text, text, text
) from public, anon, authenticated;
revoke all on function public.get_inventory_agent_variant_balances(uuid, uuid, uuid[])
  from public, anon, authenticated;
revoke all on function public.read_inventory_agent_transactions(uuid, text, integer)
  from public, anon, authenticated;

grant execute on function public.load_inventory_agent_conversation(uuid, uuid, bigint)
  to service_role;
grant execute on function public.save_inventory_agent_conversation(
  uuid, uuid, uuid, jsonb, uuid[], uuid[], text, uuid, uuid, text, text, text
) to service_role;
grant execute on function public.get_inventory_agent_variant_balances(uuid, uuid, uuid[])
  to service_role;
grant execute on function public.read_inventory_agent_transactions(uuid, text, integer)
  to service_role;

comment on table public.inventory_agent_conversations is
  'Durable model/tool history and replay metadata for one Telegram inventory conversation.';
comment on function public.load_inventory_agent_conversation(uuid, uuid, bigint) is
  'Loads or creates an actor-scoped Telegram inventory-agent conversation.';
comment on function public.save_inventory_agent_conversation(
  uuid, uuid, uuid, jsonb, uuid[], uuid[], text, uuid, uuid, text, text, text
) is 'Persists one grounded, replayable inventory-agent turn.';
