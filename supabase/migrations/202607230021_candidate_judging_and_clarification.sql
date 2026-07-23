create type public.match_clarification_status as enum (
  'awaiting_reply',
  'resolved',
  'cancelled'
);

create table public.match_clarification_requests (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  proposal_line_id uuid not null unique,
  requested_by uuid not null,
  chat_id bigint not null,
  status public.match_clarification_status not null default 'awaiting_reply',
  question text not null,
  accumulated_attributes jsonb not null default '{}'::jsonb,
  clarification_replies jsonb not null default '[]'::jsonb,
  turn_count integer not null default 0,
  last_source_event_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  resolved_at timestamptz,
  foreign key (organization_id, proposal_line_id)
    references public.proposal_lines (organization_id, id) on delete cascade,
  foreign key (organization_id, requested_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, last_source_event_id)
    references public.source_events (organization_id, id),
  check (nullif(trim(question), '') is not null),
  check (jsonb_typeof(accumulated_attributes) = 'object'),
  check (jsonb_typeof(clarification_replies) = 'array'),
  check (turn_count >= 0)
);

create index match_clarification_pending_lookup_idx
  on public.match_clarification_requests (
    requested_by,
    chat_id,
    created_at,
    id
  )
  where status = 'awaiting_reply';

alter table public.match_clarification_requests enable row level security;
revoke all on table public.match_clarification_requests from public, anon, authenticated;
grant select, insert, update, delete on public.match_clarification_requests to service_role;

create or replace function public.get_inventory_candidate_context(
  p_organization_id uuid,
  p_item_variant_ids uuid[]
)
returns table (
  item_variant_id uuid,
  item_attributes jsonb,
  variant_attributes jsonb,
  attribute_matching_roles jsonb
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select
    variant.id,
    item.attributes,
    variant.attributes,
    coalesce(
      (
        select jsonb_object_agg(
          definition.key,
          case
            when definition.validation_rules ->> 'matching_role'
              in ('discriminator', 'supporting', 'operational', 'ignored')
            then definition.validation_rules ->> 'matching_role'
            else 'supporting'
          end
        )
        from public.custom_field_definitions as definition
        where definition.organization_id = variant.organization_id
          and definition.active
          and definition.entity_type in ('item', 'variant')
      ),
      '{}'::jsonb
    )
  from public.item_variants as variant
  join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
   and item.active
  where variant.organization_id = p_organization_id
    and variant.active
    and variant.id = any(coalesce(p_item_variant_ids, array[]::uuid[]));
$$;

create or replace function public.begin_match_clarifications(
  p_proposal_id uuid,
  p_actor_id uuid,
  p_chat_id bigint
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_proposal public.transaction_proposals%rowtype;
  v_count integer;
begin
  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.id = p_proposal_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Proposal was not found';
  end if;
  if v_proposal.status <> 'pending_confirmation' then
    raise exception using errcode = '22023', message = 'Proposal is no longer pending';
  end if;
  if v_proposal.created_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_proposal.organization_id
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot begin clarification';
  end if;

  insert into public.match_clarification_requests (
    organization_id,
    proposal_line_id,
    requested_by,
    chat_id,
    question,
    accumulated_attributes
  )
  select
    line.organization_id,
    line.id,
    p_actor_id,
    p_chat_id,
    line.match_evidence ->> 'clarification_question',
    line.attributes
  from public.proposal_lines as line
  where line.proposal_id = v_proposal.id
    and line.item_variant_id is null
    and line.match_evidence ->> 'decision' = 'clarification_required'
    and nullif(trim(line.match_evidence ->> 'clarification_question'), '') is not null
  on conflict (proposal_line_id) do nothing;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

create or replace function public.find_pending_match_clarification(
  p_actor_id uuid,
  p_chat_id bigint
)
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select request.id
  from public.match_clarification_requests as request
  join public.proposal_lines as line
    on line.organization_id = request.organization_id
   and line.id = request.proposal_line_id
  join public.transaction_proposals as proposal
    on proposal.organization_id = line.organization_id
   and proposal.id = line.proposal_id
  where request.requested_by = p_actor_id
    and request.chat_id = p_chat_id
    and request.status = 'awaiting_reply'
    and proposal.status = 'pending_confirmation'
  order by request.created_at, line.line_number, request.id
  limit 1;
$$;

create or replace function public.get_match_clarification_view(p_request_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  v_result jsonb;
begin
  select jsonb_build_object(
    'request_id', request.id,
    'proposal_id', proposal.id,
    'proposal_line_id', line.id,
    'line', coalesce(
      proposal.raw_command -> 'lines' -> (line.line_number - 1),
      jsonb_build_object(
        'source_text', line.source_text,
        'item_reference', jsonb_build_object('type', 'UNKNOWN', 'value', line.extracted_description),
        'description', line.extracted_description,
        'quantity', line.requested_quantity::text,
        'unit', line.requested_unit,
        'attributes', '[]'::jsonb
      )
    ),
    'question', request.question,
    'accumulated_attributes', request.accumulated_attributes,
    'clarification_replies', request.clarification_replies,
    'candidates', coalesce(line.match_evidence -> 'candidates', '[]'::jsonb)
  ) into v_result
  from public.match_clarification_requests as request
  join public.proposal_lines as line
    on line.organization_id = request.organization_id
   and line.id = request.proposal_line_id
  join public.transaction_proposals as proposal
    on proposal.organization_id = line.organization_id
   and proposal.id = line.proposal_id
  where request.id = p_request_id;
  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Clarification request was not found';
  end if;
  return v_result;
end;
$$;

create or replace function public.apply_match_clarification_judgment(
  p_request_id uuid,
  p_event_id uuid,
  p_actor_id uuid,
  p_user_reply text,
  p_action text,
  p_selected_candidate_id uuid,
  p_question text,
  p_reason text,
  p_resolved_attributes jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.match_clarification_requests%rowtype;
  v_line public.proposal_lines%rowtype;
  v_proposal public.transaction_proposals%rowtype;
  v_candidate jsonb;
  v_item record;
  v_factor numeric(24, 8);
  v_attributes jsonb := coalesce(p_resolved_attributes, '{}'::jsonb);
begin
  if p_action not in ('SELECT', 'ASK_USER', 'NO_MATCH') then
    raise exception using errcode = '22023', message = 'Unknown clarification action';
  end if;
  if jsonb_typeof(v_attributes) <> 'object' then
    raise exception using errcode = '22023', message = 'Resolved attributes must be an object';
  end if;
  if nullif(trim(p_user_reply), '') is null then
    raise exception using errcode = '22023', message = 'Clarification reply is required';
  end if;

  select request.* into v_request
  from public.match_clarification_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Clarification request was not found';
  end if;
  if v_request.status <> 'awaiting_reply' then
    raise exception using errcode = '22023', message = 'Clarification request is not awaiting a reply';
  end if;
  if v_request.requested_by <> p_actor_id then
    raise exception using errcode = '42501', message = 'Actor cannot answer this clarification';
  end if;
  if not exists (
    select 1 from public.source_events as event
    where event.id = p_event_id
      and event.organization_id = v_request.organization_id
  ) then
    raise exception using errcode = '22023', message = 'Clarification source event is invalid';
  end if;

  select line.* into v_line
  from public.proposal_lines as line
  where line.id = v_request.proposal_line_id
  for update;
  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.id = v_line.proposal_id
  for update;
  if v_proposal.status <> 'pending_confirmation' or v_line.item_variant_id is not null then
    raise exception using errcode = '22023', message = 'Proposal line no longer needs clarification';
  end if;

  if p_action = 'ASK_USER' then
    if nullif(trim(p_question), '') is null or p_selected_candidate_id is not null then
      raise exception using errcode = '22023', message = 'A follow-up question is required';
    end if;
    update public.match_clarification_requests
    set question = trim(p_question),
        accumulated_attributes = accumulated_attributes || v_attributes,
        clarification_replies = clarification_replies || jsonb_build_array(trim(p_user_reply)),
        turn_count = turn_count + 1,
        last_source_event_id = p_event_id,
        updated_at = now()
    where id = v_request.id;
    update public.proposal_lines
    set attributes = attributes || v_attributes,
        match_evidence = match_evidence || jsonb_build_object(
          'decision', 'clarification_required',
          'clarification_question', trim(p_question),
          'reason', p_reason,
          'clarification_turn_count', v_request.turn_count + 1
        )
    where id = v_line.id;
    return v_proposal.id;
  end if;

  if p_action = 'NO_MATCH' then
    if p_selected_candidate_id is not null then
      raise exception using errcode = '22023', message = 'No-match cannot select a candidate';
    end if;
    update public.match_clarification_requests
    set status = 'resolved',
        accumulated_attributes = accumulated_attributes || v_attributes,
        clarification_replies = clarification_replies || jsonb_build_array(trim(p_user_reply)),
        turn_count = turn_count + 1,
        last_source_event_id = p_event_id,
        updated_at = now(),
        resolved_at = now()
    where id = v_request.id;
    update public.proposal_lines
    set attributes = attributes || v_attributes,
        match_evidence = (match_evidence - 'clarification_question') || jsonb_build_object(
          'decision', 'not_found',
          'reason', p_reason,
          'show_candidates', false,
          'clarification_turn_count', v_request.turn_count + 1
        )
    where id = v_line.id;
    return v_proposal.id;
  end if;

  if p_selected_candidate_id is null or p_question is not null then
    raise exception using errcode = '22023', message = 'Select requires one candidate';
  end if;
  select candidate.value into v_candidate
  from jsonb_array_elements(coalesce(v_line.match_evidence -> 'candidates', '[]')) as candidate(value)
  where candidate.value ->> 'item_variant_id' = p_selected_candidate_id::text
  limit 1;
  if v_candidate is null then
    raise exception using errcode = '22023', message = 'Selected candidate was not offered';
  end if;

  select item.base_unit, item.tracking_mode into v_item
  from public.item_variants as variant
  join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
  where variant.organization_id = v_line.organization_id
    and variant.id = p_selected_candidate_id
    and variant.active
    and item.active;
  if not found then
    raise exception using errcode = '22023', message = 'Selected candidate is not active';
  end if;
  if v_item.tracking_mode <> 'simple' then
    raise exception using errcode = '0A000', message = 'Lot or serial details are required';
  end if;
  if v_line.requested_unit is null
     or lower(trim(v_line.requested_unit)) = lower(v_item.base_unit) then
    v_factor := 1;
  else
    select conversion.factor_to_base into v_factor
    from public.item_unit_conversions as conversion
    where conversion.organization_id = v_line.organization_id
      and conversion.item_variant_id = p_selected_candidate_id
      and lower(conversion.from_unit) = lower(trim(v_line.requested_unit));
  end if;
  if v_factor is null then
    raise exception using errcode = '22023', message = 'No unit conversion exists';
  end if;

  update public.proposal_lines
  set item_variant_id = p_selected_candidate_id,
      base_quantity_delta = v_line.requested_quantity * v_factor
        * case when v_proposal.intent = 'issue_stock' then -1 else 1 end,
      base_unit = v_item.base_unit,
      match_method = coalesce(
        (v_candidate ->> 'match_method')::public.match_method,
        'semantic_rerank'::public.match_method
      ),
      match_score = coalesce((v_candidate ->> 'match_score')::numeric, 0),
      attributes = attributes || v_attributes,
      match_evidence = (match_evidence - 'clarification_question') || jsonb_build_object(
        'decision', 'matched',
        'reason', p_reason,
        'selected_item_variant_id', p_selected_candidate_id,
        'selected_after_clarification', true,
        'clarification_turn_count', v_request.turn_count + 1
      )
  where id = v_line.id;
  update public.match_clarification_requests
  set status = 'resolved',
      accumulated_attributes = accumulated_attributes || v_attributes,
      clarification_replies = clarification_replies || jsonb_build_array(trim(p_user_reply)),
      turn_count = turn_count + 1,
      last_source_event_id = p_event_id,
      updated_at = now(),
      resolved_at = now()
  where id = v_request.id;
  return v_proposal.id;
end;
$$;

create or replace function public.get_proposal_confirmation_view(p_proposal_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  v_result jsonb;
begin
  select jsonb_build_object(
    'proposal_id', proposal.id,
    'intent', proposal.intent,
    'lines', coalesce(
      jsonb_agg(
        jsonb_build_object(
          'proposal_line_id', line.id,
          'description', coalesce(line.extracted_description, line.source_text),
          'quantity', line.requested_quantity::text,
          'unit', line.requested_unit,
          'matched_label', case
            when variant.id is null then null
            else coalesce(variant.name, item.name) || ' · ' || variant.sku
          end,
          'match_decision', line.match_evidence ->> 'decision',
          'clarification_question', line.match_evidence ->> 'clarification_question',
          'show_candidates', coalesce(
            (line.match_evidence ->> 'show_candidates')::boolean,
            false
          ),
          'candidate_choices', case
            when variant.id is not null then '[]'::jsonb
            else coalesce(
              (
                select jsonb_agg(
                  jsonb_build_object(
                    'item_variant_id', candidate.value ->> 'item_variant_id',
                    'label',
                      coalesce(
                        candidate.value ->> 'variant_name',
                        candidate.value ->> 'item_name',
                        candidate.value ->> 'sku',
                        'Unknown item'
                      ) || case
                        when candidate.value ->> 'sku' is null then ''
                        else ' · ' || (candidate.value ->> 'sku')
                      end
                  )
                  order by candidate.ordinality
                )
                from jsonb_array_elements(
                  coalesce(line.match_evidence -> 'candidates', '[]'::jsonb)
                ) with ordinality as candidate(value, ordinality)
                where candidate.value ->> 'item_variant_id' is not null
              ),
              '[]'::jsonb
            )
          end
        )
        order by line.line_number
      ),
      '[]'::jsonb
    )
  ) into v_result
  from public.transaction_proposals as proposal
  join public.proposal_lines as line
    on line.organization_id = proposal.organization_id
   and line.proposal_id = proposal.id
  left join public.item_variants as variant
    on variant.organization_id = line.organization_id
   and variant.id = line.item_variant_id
  left join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
  where proposal.id = p_proposal_id
  group by proposal.id, proposal.intent;
  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Proposal confirmation view was not found';
  end if;
  return v_result;
end;
$$;

revoke all on function public.get_inventory_candidate_context(uuid, uuid[])
  from public, anon, authenticated;
revoke all on function public.begin_match_clarifications(uuid, uuid, bigint)
  from public, anon, authenticated;
revoke all on function public.find_pending_match_clarification(uuid, bigint)
  from public, anon, authenticated;
revoke all on function public.get_match_clarification_view(uuid)
  from public, anon, authenticated;
revoke all on function public.apply_match_clarification_judgment(
  uuid, uuid, uuid, text, text, uuid, text, text, jsonb
) from public, anon, authenticated;

grant execute on function public.get_inventory_candidate_context(uuid, uuid[])
  to service_role;
grant execute on function public.begin_match_clarifications(uuid, uuid, bigint)
  to service_role;
grant execute on function public.find_pending_match_clarification(uuid, bigint)
  to service_role;
grant execute on function public.get_match_clarification_view(uuid)
  to service_role;
grant execute on function public.apply_match_clarification_judgment(
  uuid, uuid, uuid, text, text, uuid, text, text, jsonb
) to service_role;

comment on table public.match_clarification_requests is
  'Durable user conversation state for resolving an ambiguous inventory candidate match.';
comment on function public.get_inventory_candidate_context(uuid, uuid[]) is
  'Returns catalog attributes and company-configured matching roles for offered candidates.';
