alter table public.inventory_agent_conversations
  add column summary text,
  add column summary_updated_at timestamptz,
  add column context_compacted_at timestamptz,
  add constraint inventory_agent_conversations_summary_length
    check (summary is null or length(summary) between 1 and 12000);

create table public.inventory_agent_turns (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  conversation_id uuid not null,
  source_event_id uuid not null,
  history jsonb not null,
  estimated_tokens integer not null,
  input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  total_tokens integer not null default 0,
  created_at timestamptz not null default now(),
  compacted_at timestamptz,
  compaction_policy text,
  foreign key (organization_id, conversation_id)
    references public.inventory_agent_conversations (organization_id, id) on delete cascade,
  foreign key (organization_id, source_event_id)
    references public.source_events (organization_id, id),
  unique (organization_id, source_event_id),
  check (jsonb_typeof(history) = 'array' and jsonb_array_length(history) > 0),
  check (estimated_tokens > 0),
  check (input_tokens >= 0 and output_tokens >= 0 and total_tokens >= 0),
  check (
    (compacted_at is null and compaction_policy is null)
    or
    (compacted_at is not null and compaction_policy in ('discard', 'summarize'))
  )
);

create index inventory_agent_turns_active_idx
  on public.inventory_agent_turns (conversation_id, created_at, id)
  where compacted_at is null;

alter table public.inventory_agent_turns enable row level security;
grant select, insert, update, delete on public.inventory_agent_turns to service_role;

insert into public.inventory_agent_turns (
  organization_id,
  conversation_id,
  source_event_id,
  history,
  estimated_tokens,
  created_at
)
select
  conversation.organization_id,
  conversation.id,
  conversation.last_source_event_id,
  conversation.history,
  greatest(1, ceil(length(conversation.history::text) / 4.0)::integer),
  conversation.updated_at
from public.inventory_agent_conversations as conversation
where conversation.last_source_event_id is not null
  and jsonb_array_length(conversation.history) > 0
on conflict (organization_id, source_event_id) do nothing;

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
  v_active_turns jsonb;
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

  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'turn_id', turn.id,
        'source_event_id', turn.source_event_id,
        'history', turn.history,
        'estimated_tokens', turn.estimated_tokens,
        'created_at', turn.created_at
      )
      order by turn.created_at, turn.id
    ),
    '[]'::jsonb
  )
  into v_active_turns
  from public.inventory_agent_turns as turn
  where turn.conversation_id = v_conversation.id
    and turn.compacted_at is null;

  return jsonb_build_object(
    'conversation_id', v_conversation.id,
    'organization_id', v_conversation.organization_id,
    'organization_user_id', v_conversation.organization_user_id,
    'chat_id', v_conversation.chat_id,
    'history', v_conversation.history,
    'allowed_variant_ids', to_jsonb(v_conversation.allowed_variant_ids),
    'allowed_transaction_ids', to_jsonb(v_conversation.allowed_transaction_ids),
    'summary', v_conversation.summary,
    'active_turns', v_active_turns,
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

create or replace function public.save_inventory_agent_conversation_turn(
  p_conversation_id uuid,
  p_source_event_id uuid,
  p_actor_id uuid,
  p_history jsonb,
  p_turn_history jsonb,
  p_estimated_tokens integer,
  p_allowed_variant_ids uuid[],
  p_allowed_transaction_ids uuid[],
  p_reply_text text,
  p_proposal_id uuid,
  p_reversal_request_id uuid,
  p_reversal_reason text,
  p_response_id text,
  p_model_name text,
  p_input_tokens integer,
  p_output_tokens integer,
  p_total_tokens integer
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_conversation_id uuid;
  v_organization_id uuid;
  v_created_at timestamptz;
begin
  if jsonb_typeof(p_turn_history) <> 'array'
    or jsonb_array_length(p_turn_history) = 0
  then
    raise exception using errcode = '22023', message = 'Agent turn history must be a non-empty array';
  end if;
  if p_estimated_tokens is null or p_estimated_tokens <= 0 then
    raise exception using errcode = '22023', message = 'Agent turn token estimate must be positive';
  end if;
  if least(p_input_tokens, p_output_tokens, p_total_tokens) < 0 then
    raise exception using errcode = '22023', message = 'Agent token usage cannot be negative';
  end if;

  v_conversation_id := public.save_inventory_agent_conversation(
    p_conversation_id,
    p_source_event_id,
    p_actor_id,
    p_history,
    p_allowed_variant_ids,
    p_allowed_transaction_ids,
    p_reply_text,
    p_proposal_id,
    p_reversal_request_id,
    p_reversal_reason,
    p_response_id,
    p_model_name
  );

  select conversation.organization_id
  into v_organization_id
  from public.inventory_agent_conversations as conversation
  where conversation.id = v_conversation_id;

  select source_event.received_at
  into v_created_at
  from public.source_events as source_event
  where source_event.organization_id = v_organization_id
    and source_event.id = p_source_event_id;

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
  values (
    v_organization_id,
    v_conversation_id,
    p_source_event_id,
    p_turn_history,
    p_estimated_tokens,
    coalesce(p_input_tokens, 0),
    coalesce(p_output_tokens, 0),
    coalesce(p_total_tokens, 0),
    coalesce(v_created_at, now())
  )
  on conflict (organization_id, source_event_id) do nothing;

  return v_conversation_id;
end;
$$;

create or replace function public.compact_inventory_agent_conversation(
  p_conversation_id uuid,
  p_actor_id uuid,
  p_turn_ids uuid[],
  p_policy text,
  p_summary text
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_conversation public.inventory_agent_conversations%rowtype;
  v_history jsonb;
  v_requested_count integer;
  v_matching_count integer;
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
    raise exception using errcode = '42501', message = 'Actor cannot compact this conversation';
  end if;
  if p_policy not in ('discard', 'summarize') then
    raise exception using errcode = '22023', message = 'Unknown context compaction policy';
  end if;
  if p_policy = 'summarize' and nullif(trim(p_summary), '') is null then
    raise exception using errcode = '22023', message = 'Summary policy requires a summary';
  end if;

  v_requested_count := coalesce(cardinality(p_turn_ids), 0);
  if v_requested_count = 0 then
    raise exception using errcode = '22023', message = 'Context compaction requires turn IDs';
  end if;
  if (
    select count(distinct requested.turn_id)
    from unnest(p_turn_ids) as requested(turn_id)
  ) <> v_requested_count then
    raise exception using errcode = '22023', message = 'Context compaction turn IDs must be unique';
  end if;

  select count(*) into v_matching_count
  from public.inventory_agent_turns as turn
  where turn.conversation_id = v_conversation.id
    and turn.id = any(p_turn_ids)
    and turn.compacted_at is null;
  if v_matching_count <> v_requested_count then
    raise exception using errcode = '22023', message = 'Context compaction contains unavailable turns';
  end if;

  update public.inventory_agent_turns
  set compacted_at = now(),
      compaction_policy = p_policy
  where conversation_id = v_conversation.id
    and id = any(p_turn_ids)
    and compacted_at is null;

  select coalesce(
    jsonb_agg(item.value order by turn.created_at, turn.id, item.ordinality),
    '[]'::jsonb
  )
  into v_history
  from public.inventory_agent_turns as turn
  cross join lateral jsonb_array_elements(turn.history)
    with ordinality as item(value, ordinality)
  where turn.conversation_id = v_conversation.id
    and turn.compacted_at is null;

  update public.inventory_agent_conversations
  set history = v_history,
      summary = case
        when p_policy = 'summarize' then nullif(trim(p_summary), '')
        else null
      end,
      summary_updated_at = case
        when p_policy = 'summarize' then now()
        else null
      end,
      context_compacted_at = now(),
      allowed_variant_ids = '{}'::uuid[],
      allowed_transaction_ids = '{}'::uuid[],
      updated_at = now()
  where id = v_conversation.id;

  return v_conversation.id;
end;
$$;

revoke all on table public.inventory_agent_turns from public, anon, authenticated;
revoke all on function public.save_inventory_agent_conversation_turn(
  uuid, uuid, uuid, jsonb, jsonb, integer, uuid[], uuid[], text, uuid, uuid,
  text, text, text, integer, integer, integer
) from public, anon, authenticated;
revoke all on function public.compact_inventory_agent_conversation(
  uuid, uuid, uuid[], text, text
) from public, anon, authenticated;

grant execute on function public.save_inventory_agent_conversation_turn(
  uuid, uuid, uuid, jsonb, jsonb, integer, uuid[], uuid[], text, uuid, uuid,
  text, text, text, integer, integer, integer
) to service_role;
grant execute on function public.compact_inventory_agent_conversation(
  uuid, uuid, uuid[], text, text
) to service_role;

comment on table public.inventory_agent_turns is
  'Immutable per-turn model/tool history retained for audit after active context compaction.';
comment on function public.save_inventory_agent_conversation_turn(
  uuid, uuid, uuid, jsonb, jsonb, integer, uuid[], uuid[], text, uuid, uuid,
  text, text, text, integer, integer, integer
) is 'Atomically persists an agent conversation and its immutable turn boundary.';
comment on function public.compact_inventory_agent_conversation(
  uuid, uuid, uuid[], text, text
) is 'Excludes selected turns from active model context without deleting their audit rows.';
