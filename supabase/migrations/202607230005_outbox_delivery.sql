drop index public.processing_outbox_pending_idx;

alter table public.processing_outbox
  alter column status drop default;

create type public.outbox_delivery_status as enum ('pending', 'sending', 'sent', 'failed');

alter table public.processing_outbox
  alter column status type public.outbox_delivery_status
  using status::text::public.outbox_delivery_status;

drop type public.outbox_status;
alter type public.outbox_delivery_status rename to outbox_status;

alter table public.processing_outbox
  alter column status set default 'pending'::public.outbox_status,
  add column delivery_started_at timestamptz,
  add column next_attempt_at timestamptz not null default now();

create index processing_outbox_pending_idx
  on public.processing_outbox (next_attempt_at, created_at, id)
  where status = 'pending';

create or replace function public.claim_processing_outbox(p_outbox_id uuid default null)
returns table (
  outbox_id uuid,
  organization_id uuid,
  source_event_id uuid,
  outcome_type public.processing_outcome_type,
  aggregate_id uuid,
  chat_id bigint,
  payload jsonb,
  attempt_number integer
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  return query
  with candidate as (
    select outbox.id
    from public.processing_outbox as outbox
    join public.source_events as source_event
      on source_event.organization_id = outbox.organization_id
     and source_event.id = outbox.source_event_id
    where (p_outbox_id is null or outbox.id = p_outbox_id)
      and source_event.status = 'processed'
      and (
        (outbox.status = 'pending' and outbox.next_attempt_at <= now())
        or (
          outbox.status = 'sending'
          and outbox.delivery_started_at < now() - interval '5 minutes'
        )
      )
    order by outbox.next_attempt_at, outbox.created_at, outbox.id
    for update of outbox skip locked
    limit 1
  ),
  claimed as (
    update public.processing_outbox as outbox
    set status = 'sending',
        delivery_started_at = now(),
        attempts = outbox.attempts + 1,
        error_message = null
    from candidate
    where outbox.id = candidate.id
    returning outbox.*
  )
  select
    claimed.id,
    claimed.organization_id,
    claimed.source_event_id,
    claimed.outcome_type,
    claimed.aggregate_id,
    claimed.chat_id,
    claimed.payload,
    claimed.attempts
  from claimed;
end;
$$;

create or replace function public.finish_processing_outbox(
  p_outbox_id uuid,
  p_success boolean,
  p_error_message text default null,
  p_retry_delay_seconds integer default 30
)
returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_outbox public.processing_outbox%rowtype;
begin
  if p_retry_delay_seconds < 0 then
    raise exception using errcode = '22023', message = 'Retry delay cannot be negative';
  end if;
  if not p_success and nullif(trim(p_error_message), '') is null then
    raise exception using errcode = '22023', message = 'Failed deliveries require an error message';
  end if;

  select outbox.* into v_outbox
  from public.processing_outbox as outbox
  where outbox.id = p_outbox_id
  for update;

  if not found or v_outbox.status <> 'sending' then
    return null;
  end if;

  if p_success then
    update public.processing_outbox
    set status = 'sent',
        error_message = null,
        sent_at = now()
    where id = p_outbox_id;
    return 'sent';
  end if;

  if v_outbox.attempts >= 5 then
    update public.processing_outbox
    set status = 'failed',
        error_message = left(trim(p_error_message), 1000),
        delivery_started_at = null
    where id = p_outbox_id;
    return 'failed';
  end if;

  update public.processing_outbox
  set status = 'pending',
      error_message = left(trim(p_error_message), 1000),
      delivery_started_at = null,
      next_attempt_at = now() + make_interval(secs => p_retry_delay_seconds)
  where id = p_outbox_id;
  return 'pending';
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

revoke all on function public.claim_processing_outbox(uuid)
  from public, anon, authenticated;
revoke all on function public.finish_processing_outbox(uuid, boolean, text, integer)
  from public, anon, authenticated;
revoke all on function public.get_proposal_confirmation_view(uuid)
  from public, anon, authenticated;

grant execute on function public.claim_processing_outbox(uuid) to service_role;
grant execute on function public.finish_processing_outbox(uuid, boolean, text, integer)
  to service_role;
grant execute on function public.get_proposal_confirmation_view(uuid) to service_role;

comment on function public.claim_processing_outbox(uuid) is
  'Claims one due outbound outcome with skip-locked concurrency and stale-claim recovery.';
comment on function public.finish_processing_outbox(uuid, boolean, text, integer) is
  'Marks delivery sent, schedules a retry, or dead-letters it after five attempts.';
comment on function public.get_proposal_confirmation_view(uuid) is
  'Builds the resolved and candidate line data needed for Telegram proposal review.';
