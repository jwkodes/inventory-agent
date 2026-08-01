alter table public.inventory_agent_turns
  add column cached_input_tokens integer not null default 0,
  add column cache_write_tokens integer not null default 0,
  add constraint inventory_agent_turns_cached_input_tokens_valid
    check (cached_input_tokens >= 0 and cached_input_tokens <= input_tokens),
  add constraint inventory_agent_turns_cache_write_tokens_valid
    check (cache_write_tokens >= 0 and cache_write_tokens <= input_tokens);

drop function public.save_inventory_agent_conversation_turn(
  uuid, uuid, uuid, jsonb, jsonb, integer, uuid[], uuid[], text, uuid, uuid,
  text, text, text, integer, integer, integer
);

create function public.save_inventory_agent_conversation_turn(
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
  p_cached_input_tokens integer,
  p_cache_write_tokens integer,
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
    raise exception using
      errcode = '22023',
      message = 'Agent turn history must be a non-empty array';
  end if;
  if p_estimated_tokens is null or p_estimated_tokens <= 0 then
    raise exception using
      errcode = '22023',
      message = 'Agent turn token estimate must be positive';
  end if;
  if least(
    p_input_tokens,
    p_cached_input_tokens,
    p_cache_write_tokens,
    p_output_tokens,
    p_total_tokens
  ) < 0 then
    raise exception using errcode = '22023', message = 'Agent token usage cannot be negative';
  end if;
  if p_cached_input_tokens > p_input_tokens
    or p_cache_write_tokens > p_input_tokens
  then
    raise exception using
      errcode = '22023',
      message = 'Agent cache usage cannot exceed input usage';
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
    cached_input_tokens,
    cache_write_tokens,
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
    coalesce(p_cached_input_tokens, 0),
    coalesce(p_cache_write_tokens, 0),
    coalesce(p_output_tokens, 0),
    coalesce(p_total_tokens, 0),
    coalesce(v_created_at, now())
  )
  on conflict (organization_id, source_event_id) do nothing;

  return v_conversation_id;
end;
$$;

revoke all on function public.save_inventory_agent_conversation_turn(
  uuid, uuid, uuid, jsonb, jsonb, integer, uuid[], uuid[], text, uuid, uuid,
  text, text, text, integer, integer, integer, integer, integer
) from public, anon, authenticated;
grant execute on function public.save_inventory_agent_conversation_turn(
  uuid, uuid, uuid, jsonb, jsonb, integer, uuid[], uuid[], text, uuid, uuid,
  text, text, text, integer, integer, integer, integer, integer
) to service_role;

comment on column public.inventory_agent_turns.cached_input_tokens is
  'OpenAI input tokens served from prompt cache, summed across model calls in the turn.';
comment on column public.inventory_agent_turns.cache_write_tokens is
  'OpenAI prompt-cache write tokens, summed across model calls in the turn.';
comment on function public.save_inventory_agent_conversation_turn(
  uuid, uuid, uuid, jsonb, jsonb, integer, uuid[], uuid[], text, uuid, uuid,
  text, text, text, integer, integer, integer, integer, integer
) is 'Atomically persists an agent turn with prompt-cache usage observability.';
