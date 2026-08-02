alter table public.processing_outbox
  drop constraint processing_outbox_aggregate_check,
  add constraint processing_outbox_aggregate_check check (
    (
      outcome_type in (
        'proposal_ready',
        'transaction_applied',
        'catalog_item_details_required',
        'catalog_item_confirmation',
        'catalog_item_edit_confirmation',
        'catalog_batch_details_required',
        'catalog_batch_confirmation',
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
        'catalog_item_edit_confirmation',
        'catalog_batch_details_required',
        'catalog_batch_confirmation',
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
set search_path = public, pg_temp
as $$
declare
  v_simulation jsonb;
begin
  if p_outcome_type = 'catalog_item_edit_confirmation'
    and not exists (
      select 1
      from public.catalog_item_edit_requests as request
      where request.organization_id = p_organization_id
        and request.id = p_aggregate_id
        and request.chat_id = p_chat_id
        and request.status = 'awaiting_confirmation'
    )
  then
    raise exception using errcode = '22023',
      message = 'Catalog edit request state does not match outcome';
  end if;

  select source_event.payload -> '_inventory_agent_dev_simulation'
  into v_simulation
  from public.source_events as source_event
  where source_event.organization_id = p_organization_id
    and source_event.id = p_source_event_id;

  return public.enqueue_processing_outcome_internal(
    p_organization_id,
    p_source_event_id,
    p_outcome_type,
    p_aggregate_id,
    p_chat_id,
    coalesce(p_payload, '{}'::jsonb)
      || case
           when v_simulation is null then '{}'::jsonb
           else jsonb_build_object('_dev_simulation', v_simulation)
         end
  );
end;
$$;

revoke all on function public.enqueue_processing_outcome(
  uuid,
  uuid,
  public.processing_outcome_type,
  uuid,
  bigint,
  jsonb
) from public, anon, authenticated;
grant execute on function public.enqueue_processing_outcome(
  uuid,
  uuid,
  public.processing_outcome_type,
  uuid,
  bigint,
  jsonb
) to service_role;

comment on constraint processing_outbox_aggregate_check
  on public.processing_outbox is
  'Requires aggregate IDs for every durable workflow view, including catalog item edits.';
