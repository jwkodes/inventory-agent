alter table public.processing_outbox
  drop constraint processing_outbox_aggregate_check,
  add constraint processing_outbox_aggregate_check check (
    (
      outcome_type in (
        'proposal_ready',
        'transaction_applied',
        'reversal_reason_required',
        'reversal_confirmation'
      )
      and aggregate_id is not null
    )
    or (
      outcome_type not in (
        'proposal_ready',
        'transaction_applied',
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
    where proposal.organization_id = p_organization_id
      and proposal.id = p_aggregate_id
  ) then
    raise exception using errcode = '22023', message = 'Proposal is not in the organization';
  end if;

  if p_outcome_type = 'transaction_applied' and not exists (
    select 1 from public.inventory_transactions as transaction
    where transaction.organization_id = p_organization_id
      and transaction.id = p_aggregate_id
      and transaction.status = 'applied'
  ) then
    raise exception using
      errcode = '22023',
      message = 'Applied transaction is not in the organization';
  end if;

  if p_outcome_type = 'callback_notice'
    and nullif(trim(p_payload ->> 'message'), '') is null
  then
    raise exception using
      errcode = '22023',
      message = 'Callback notice requires a message';
  end if;

  if p_outcome_type = 'reversal_reason_required' and not exists (
    select 1 from public.transaction_reversal_requests as request
    where request.organization_id = p_organization_id
      and request.id = p_aggregate_id
      and request.status = 'awaiting_reason'
      and request.chat_id = p_chat_id
  ) then
    raise exception using
      errcode = '22023',
      message = 'Pending reversal reason request does not match organization or chat';
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

comment on function public.enqueue_processing_outcome(
  uuid, uuid, public.processing_outcome_type, uuid, bigint, jsonb
) is 'Creates one tenant-bound, idempotent outbound processing result.';
