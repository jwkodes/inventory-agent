create type public.reversal_request_status as enum (
  'awaiting_reason',
  'awaiting_confirmation',
  'completed',
  'cancelled'
);

create table public.transaction_reversal_requests (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  transaction_id uuid not null,
  requested_by uuid not null,
  chat_id bigint not null,
  status public.reversal_request_status not null default 'awaiting_reason',
  reason text,
  reason_source_event_id uuid,
  reversal_transaction_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  foreign key (organization_id, transaction_id)
    references public.inventory_transactions (organization_id, id),
  foreign key (organization_id, requested_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, reason_source_event_id)
    references public.source_events (organization_id, id),
  foreign key (organization_id, reversal_transaction_id)
    references public.inventory_transactions (organization_id, id),
  unique (transaction_id),
  unique (reason_source_event_id),
  check (reason is null or length(reason) between 1 and 1000),
  check (
    (
      status = 'awaiting_reason'
      and reason is null
      and reason_source_event_id is null
      and reversal_transaction_id is null
      and completed_at is null
    )
    or (
      status = 'awaiting_confirmation'
      and reason is not null
      and reason_source_event_id is not null
      and reversal_transaction_id is null
      and completed_at is null
    )
    or (
      status = 'completed'
      and reason is not null
      and reason_source_event_id is not null
      and reversal_transaction_id is not null
      and completed_at is not null
    )
    or (
      status = 'cancelled'
      and reversal_transaction_id is null
      and completed_at is not null
    )
  )
);

create unique index transaction_reversal_requests_one_reason_prompt_idx
  on public.transaction_reversal_requests (organization_id, requested_by, chat_id)
  where status = 'awaiting_reason';

alter table public.transaction_reversal_requests enable row level security;
grant select, insert, update, delete on public.transaction_reversal_requests to service_role;

alter table public.processing_outbox
  drop constraint processing_outbox_check,
  add constraint processing_outbox_aggregate_check check (
    (
      outcome_type in ('proposal_ready', 'reversal_confirmation')
      and aggregate_id is not null
    )
    or (
      outcome_type not in ('proposal_ready', 'reversal_confirmation')
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
    where proposal.organization_id = p_organization_id
      and proposal.id = p_aggregate_id
  ) then
    raise exception using errcode = '22023', message = 'Proposal is not in the organization';
  end if;

  if p_outcome_type = 'reversal_confirmation' and not exists (
    select 1 from public.transaction_reversal_requests as request
    where request.organization_id = p_organization_id
      and request.id = p_aggregate_id
      and request.status = 'awaiting_confirmation'
      and request.chat_id = p_chat_id
      and request.reason = nullif(trim(p_payload ->> 'reason'), '')
  ) then
    raise exception using
      errcode = '22023',
      message = 'Pending reversal confirmation does not match organization, chat, or reason';
  end if;

  insert into public.processing_outbox (
    organization_id,
    source_event_id,
    outcome_type,
    aggregate_id,
    chat_id,
    payload
  )
  values (
    p_organization_id,
    p_source_event_id,
    p_outcome_type,
    p_aggregate_id,
    p_chat_id,
    coalesce(p_payload, '{}'::jsonb)
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

create or replace function public.begin_transaction_reversal_request(
  p_transaction_id uuid,
  p_actor_id uuid,
  p_chat_id bigint
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_transaction public.inventory_transactions%rowtype;
  v_request public.transaction_reversal_requests%rowtype;
begin
  select transaction.* into v_transaction
  from public.inventory_transactions as transaction
  where transaction.id = p_transaction_id
  for update;

  if not found then
    raise exception using errcode = 'P0002', message = 'Inventory transaction was not found';
  end if;
  if v_transaction.transaction_type = 'reversal' then
    raise exception using errcode = '22023', message = 'A reversal cannot itself be reversed';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_transaction.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using
      errcode = '42501',
      message = 'Only an active manager or admin can request a reversal';
  end if;

  select request.* into v_request
  from public.transaction_reversal_requests as request
  where request.transaction_id = p_transaction_id
  for update;

  if found and v_request.status <> 'cancelled' then
    return v_request.id;
  end if;

  if exists (
    select 1 from public.inventory_transactions as reversal
    where reversal.reversal_of_transaction_id = p_transaction_id
  ) then
    raise exception using errcode = '22023', message = 'Transaction is already reversed';
  end if;

  update public.transaction_reversal_requests
  set status = 'cancelled',
      completed_at = now(),
      updated_at = now()
  where organization_id = v_transaction.organization_id
    and requested_by = p_actor_id
    and chat_id = p_chat_id
    and status = 'awaiting_reason'
    and transaction_id <> p_transaction_id;

  if v_request.id is not null then
    update public.transaction_reversal_requests
    set requested_by = p_actor_id,
        chat_id = p_chat_id,
        status = 'awaiting_reason',
        reason = null,
        reason_source_event_id = null,
        reversal_transaction_id = null,
        completed_at = null,
        updated_at = now()
    where id = v_request.id;
    return v_request.id;
  end if;

  insert into public.transaction_reversal_requests (
    organization_id,
    transaction_id,
    requested_by,
    chat_id
  )
  values (
    v_transaction.organization_id,
    p_transaction_id,
    p_actor_id,
    p_chat_id
  )
  returning id into v_request.id;

  return v_request.id;
end;
$$;

create or replace function public.capture_transaction_reversal_reason(
  p_event_id uuid,
  p_actor_id uuid,
  p_chat_id bigint,
  p_reason text
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_event public.source_events%rowtype;
  v_request public.transaction_reversal_requests%rowtype;
  v_reason text;
begin
  v_reason := trim(p_reason);
  if v_reason is null or length(v_reason) = 0 or length(v_reason) > 1000 then
    raise exception using
      errcode = '22023',
      message = 'A reversal reason between 1 and 1000 characters is required';
  end if;

  select source_event.* into v_event
  from public.source_events as source_event
  where source_event.id = p_event_id
    and source_event.status = 'processing'
    and source_event.provider = 'telegram'
    and source_event.event_type = 'message';

  if not found then
    raise exception using errcode = '22023', message = 'A claimed Telegram message is required';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_event.organization_id
      and member.active
      and member.telegram_user_id = (v_event.payload #>> '{message,from,id}')::bigint
  ) then
    raise exception using errcode = '42501', message = 'Message actor does not match the event';
  end if;
  if (v_event.payload #>> '{message,chat,id}')::bigint <> p_chat_id then
    raise exception using errcode = '22023', message = 'Message chat does not match the event';
  end if;

  select request.* into v_request
  from public.transaction_reversal_requests as request
  where request.organization_id = v_event.organization_id
    and request.requested_by = p_actor_id
    and request.chat_id = p_chat_id
    and request.reason_source_event_id = p_event_id
  for update;

  if found then
    return v_request.id;
  end if;

  select request.* into v_request
  from public.transaction_reversal_requests as request
  where request.organization_id = v_event.organization_id
    and request.requested_by = p_actor_id
    and request.chat_id = p_chat_id
    and request.status = 'awaiting_reason'
  order by request.created_at desc, request.id
  for update
  limit 1;

  if not found then
    return null;
  end if;

  update public.transaction_reversal_requests
  set status = 'awaiting_confirmation',
      reason = v_reason,
      reason_source_event_id = p_event_id,
      updated_at = now()
  where id = v_request.id;

  return v_request.id;
end;
$$;

create or replace function public.confirm_transaction_reversal_request(
  p_request_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.transaction_reversal_requests%rowtype;
  v_reversal_id uuid;
begin
  select request.* into v_request
  from public.transaction_reversal_requests as request
  where request.id = p_request_id
  for update;

  if not found then
    raise exception using errcode = 'P0002', message = 'Reversal request was not found';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_request.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot confirm this reversal';
  end if;
  if v_request.status = 'completed' then
    return v_request.reversal_transaction_id;
  end if;
  if v_request.status <> 'awaiting_confirmation' then
    raise exception using errcode = '22023', message = 'Reversal request is not awaiting confirmation';
  end if;

  v_reversal_id := public.reverse_inventory_transaction(
    v_request.transaction_id,
    p_actor_id,
    v_request.reason
  );

  update public.transaction_reversal_requests
  set status = 'completed',
      reversal_transaction_id = v_reversal_id,
      completed_at = now(),
      updated_at = now()
  where id = p_request_id;

  return v_reversal_id;
end;
$$;

create or replace function public.cancel_transaction_reversal_request(
  p_request_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.transaction_reversal_requests%rowtype;
begin
  select request.* into v_request
  from public.transaction_reversal_requests as request
  where request.id = p_request_id
  for update;

  if not found then
    raise exception using errcode = 'P0002', message = 'Reversal request was not found';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_request.organization_id
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot cancel this reversal';
  end if;
  if v_request.status = 'cancelled' then
    return v_request.id;
  end if;
  if v_request.status = 'completed' then
    raise exception using errcode = '22023', message = 'A completed reversal cannot be cancelled';
  end if;

  update public.transaction_reversal_requests
  set status = 'cancelled',
      completed_at = now(),
      updated_at = now()
  where id = p_request_id;

  return p_request_id;
end;
$$;

revoke all on function public.begin_transaction_reversal_request(uuid, uuid, bigint)
  from public, anon, authenticated;
revoke all on function public.capture_transaction_reversal_reason(uuid, uuid, bigint, text)
  from public, anon, authenticated;
revoke all on function public.confirm_transaction_reversal_request(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.cancel_transaction_reversal_request(uuid, uuid)
  from public, anon, authenticated;

grant execute on function public.begin_transaction_reversal_request(uuid, uuid, bigint)
  to service_role;
grant execute on function public.capture_transaction_reversal_reason(uuid, uuid, bigint, text)
  to service_role;
grant execute on function public.confirm_transaction_reversal_request(uuid, uuid)
  to service_role;
grant execute on function public.cancel_transaction_reversal_request(uuid, uuid)
  to service_role;

comment on table public.transaction_reversal_requests is
  'Durable Telegram conversation state for a complete transaction reversal.';
comment on function public.begin_transaction_reversal_request(uuid, uuid, bigint) is
  'Starts or resumes reason collection for an authorized complete reversal.';
comment on function public.capture_transaction_reversal_reason(uuid, uuid, bigint, text) is
  'Consumes a claimed Telegram message as the pending reversal reason when applicable.';
comment on function public.confirm_transaction_reversal_request(uuid, uuid) is
  'Applies a confirmed reversal request through the immutable compensating ledger function.';
comment on function public.cancel_transaction_reversal_request(uuid, uuid) is
  'Cancels a pending reversal request without changing inventory.';
