create type public.command_clarification_status as enum (
  'awaiting_reply',
  'resolved',
  'cancelled'
);

create table public.command_clarification_requests (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  requested_by uuid not null,
  chat_id bigint not null,
  source_event_id uuid not null unique,
  status public.command_clarification_status not null default 'awaiting_reply',
  question text not null,
  extraction jsonb not null,
  clarification_replies jsonb not null default '[]'::jsonb,
  last_source_event_id uuid,
  proposal_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  resolved_at timestamptz,
  foreign key (organization_id, requested_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, source_event_id)
    references public.source_events (organization_id, id) on delete cascade,
  foreign key (organization_id, last_source_event_id)
    references public.source_events (organization_id, id) on delete cascade,
  foreign key (organization_id, proposal_id)
    references public.transaction_proposals (organization_id, id) on delete cascade,
  check (nullif(trim(question), '') is not null),
  check (jsonb_typeof(extraction) = 'object'),
  check (jsonb_typeof(clarification_replies) = 'array'),
  check (
    (status = 'awaiting_reply' and resolved_at is null and proposal_id is null)
    or (status = 'resolved' and resolved_at is not null)
    or (status = 'cancelled' and resolved_at is not null and proposal_id is null)
  )
);

create index command_clarification_pending_lookup_idx
  on public.command_clarification_requests (
    requested_by,
    chat_id,
    created_at,
    id
  )
  where status = 'awaiting_reply';

alter table public.command_clarification_requests enable row level security;
revoke all on table public.command_clarification_requests from public, anon, authenticated;
grant select, insert, update, delete on public.command_clarification_requests to service_role;

create or replace function public.begin_command_clarification(
  p_source_event_id uuid,
  p_actor_id uuid,
  p_chat_id bigint,
  p_question text,
  p_extraction jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_organization_id uuid;
  v_request_id uuid;
begin
  if nullif(trim(p_question), '') is null then
    raise exception using errcode = '22023', message = 'Clarification question is required';
  end if;
  if jsonb_typeof(p_extraction) <> 'object' then
    raise exception using errcode = '22023', message = 'Clarification extraction must be an object';
  end if;

  select source_event.organization_id into v_organization_id
  from public.source_events as source_event
  where source_event.id = p_source_event_id;
  if v_organization_id is null then
    raise exception using errcode = 'P0002', message = 'Clarification source event was not found';
  end if;
  if not exists (
    select 1
    from public.organization_users as member
    where member.organization_id = v_organization_id
      and member.id = p_actor_id
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot begin command clarification';
  end if;

  insert into public.command_clarification_requests (
    organization_id,
    requested_by,
    chat_id,
    source_event_id,
    question,
    extraction
  )
  values (
    v_organization_id,
    p_actor_id,
    p_chat_id,
    p_source_event_id,
    trim(p_question),
    p_extraction
  )
  on conflict (source_event_id) do update
  set updated_at = public.command_clarification_requests.updated_at
  returning id into v_request_id;

  return v_request_id;
end;
$$;

create or replace function public.find_pending_command_clarification(
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
  from public.command_clarification_requests as request
  join public.organization_users as member
    on member.organization_id = request.organization_id
   and member.id = request.requested_by
   and member.active
  where request.requested_by = p_actor_id
    and request.chat_id = p_chat_id
    and request.status = 'awaiting_reply'
  order by request.created_at, request.id
  limit 1;
$$;

create or replace function public.get_command_clarification_view(p_request_id uuid)
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
    'organization_id', request.organization_id,
    'requested_by', request.requested_by,
    'chat_id', request.chat_id,
    'source_event_id', request.source_event_id,
    'question', request.question,
    'extraction', request.extraction,
    'clarification_replies', request.clarification_replies
  ) into v_result
  from public.command_clarification_requests as request
  where request.id = p_request_id
    and request.status = 'awaiting_reply';

  if v_result is null then
    raise exception using errcode = 'P0002', message = 'Pending command clarification was not found';
  end if;
  return v_result;
end;
$$;

create or replace function public.continue_command_clarification(
  p_request_id uuid,
  p_event_id uuid,
  p_actor_id uuid,
  p_user_reply text,
  p_question text,
  p_extraction jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.command_clarification_requests%rowtype;
begin
  if nullif(trim(p_user_reply), '') is null
     or nullif(trim(p_question), '') is null
     or jsonb_typeof(p_extraction) <> 'object' then
    raise exception using errcode = '22023', message = 'A reply, question, and extraction are required';
  end if;

  select request.* into v_request
  from public.command_clarification_requests as request
  where request.id = p_request_id
  for update;
  if not found or v_request.status <> 'awaiting_reply' then
    raise exception using errcode = '22023', message = 'Command clarification is not awaiting a reply';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1
    from public.source_events as source_event
    where source_event.organization_id = v_request.organization_id
      and source_event.id = p_event_id
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot continue command clarification';
  end if;

  update public.command_clarification_requests
  set question = trim(p_question),
      extraction = p_extraction,
      clarification_replies = clarification_replies || jsonb_build_array(trim(p_user_reply)),
      last_source_event_id = p_event_id,
      updated_at = now()
  where id = v_request.id;
  return v_request.id;
end;
$$;

create or replace function public.resolve_command_clarification(
  p_request_id uuid,
  p_event_id uuid,
  p_actor_id uuid,
  p_user_reply text,
  p_extraction jsonb,
  p_proposal_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.command_clarification_requests%rowtype;
begin
  if nullif(trim(p_user_reply), '') is null or jsonb_typeof(p_extraction) <> 'object' then
    raise exception using errcode = '22023', message = 'A reply and extraction are required';
  end if;

  select request.* into v_request
  from public.command_clarification_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Command clarification was not found';
  end if;
  if v_request.status = 'resolved'
     and v_request.last_source_event_id = p_event_id
     and v_request.proposal_id is not distinct from p_proposal_id then
    return v_request.id;
  end if;
  if v_request.status <> 'awaiting_reply' then
    raise exception using errcode = '22023', message = 'Command clarification is not awaiting a reply';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1
    from public.source_events as source_event
    where source_event.organization_id = v_request.organization_id
      and source_event.id = p_event_id
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot resolve command clarification';
  end if;
  if p_proposal_id is not null and not exists (
    select 1
    from public.transaction_proposals as proposal
    where proposal.organization_id = v_request.organization_id
      and proposal.id = p_proposal_id
      and proposal.source_event_id = p_event_id
      and proposal.created_by = p_actor_id
  ) then
    raise exception using errcode = '22023', message = 'Clarification proposal is invalid';
  end if;

  update public.command_clarification_requests
  set status = 'resolved',
      extraction = p_extraction,
      clarification_replies = clarification_replies || jsonb_build_array(trim(p_user_reply)),
      last_source_event_id = p_event_id,
      proposal_id = p_proposal_id,
      updated_at = now(),
      resolved_at = now()
  where id = v_request.id;
  return v_request.id;
end;
$$;

revoke all on function public.begin_command_clarification(
  uuid, uuid, bigint, text, jsonb
) from public, anon, authenticated;
revoke all on function public.find_pending_command_clarification(
  uuid, bigint
) from public, anon, authenticated;
revoke all on function public.get_command_clarification_view(
  uuid
) from public, anon, authenticated;
revoke all on function public.continue_command_clarification(
  uuid, uuid, uuid, text, text, jsonb
) from public, anon, authenticated;
revoke all on function public.resolve_command_clarification(
  uuid, uuid, uuid, text, jsonb, uuid
) from public, anon, authenticated;

grant execute on function public.begin_command_clarification(
  uuid, uuid, bigint, text, jsonb
) to service_role;
grant execute on function public.find_pending_command_clarification(
  uuid, bigint
) to service_role;
grant execute on function public.get_command_clarification_view(
  uuid
) to service_role;
grant execute on function public.continue_command_clarification(
  uuid, uuid, uuid, text, text, jsonb
) to service_role;
grant execute on function public.resolve_command_clarification(
  uuid, uuid, uuid, text, jsonb, uuid
) to service_role;

comment on table public.command_clarification_requests is
  'Durable extracted invoice/text commands awaiting a natural-language clarification reply.';
