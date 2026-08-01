alter table public.transaction_reversal_requests
  add column replacement_proposal_id uuid,
  add constraint transaction_reversal_requests_replacement_proposal_fkey
    foreign key (organization_id, replacement_proposal_id)
    references public.transaction_proposals (organization_id, id);

create unique index transaction_reversal_requests_replacement_proposal_idx
  on public.transaction_reversal_requests (replacement_proposal_id)
  where replacement_proposal_id is not null;

create or replace function public.reset_reversal_replacement_on_reuse()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if old.status = 'cancelled' and new.status = 'awaiting_reason' then
    new.replacement_proposal_id := null;
  end if;
  return new;
end;
$$;

create trigger reset_reversal_replacement_on_reuse
before update on public.transaction_reversal_requests
for each row execute function public.reset_reversal_replacement_on_reuse();

create or replace function public.reject_reversal_replacement_on_cancel()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if new.status = 'cancelled'
     and old.status <> 'cancelled'
     and new.replacement_proposal_id is not null then
    update public.transaction_proposals
    set status = 'rejected'
    where organization_id = new.organization_id
      and id = new.replacement_proposal_id
      and status = 'pending_confirmation';
  end if;
  return new;
end;
$$;

create trigger reject_reversal_replacement_on_cancel
after update on public.transaction_reversal_requests
for each row execute function public.reject_reversal_replacement_on_cancel();

create or replace function public.attach_transaction_reversal_replacement(
  p_request_id uuid,
  p_proposal_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.transaction_reversal_requests%rowtype;
  v_proposal public.transaction_proposals%rowtype;
begin
  select request.* into v_request
  from public.transaction_reversal_requests as request
  where request.id = p_request_id
  for update;

  if not found then
    raise exception using errcode = 'P0002', message = 'Reversal request was not found';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1
    from public.organization_users as member
    where member.organization_id = v_request.organization_id
      and member.id = p_actor_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot link this correction';
  end if;
  if v_request.status <> 'awaiting_confirmation' then
    raise exception using
      errcode = '22023',
      message = 'Reversal request is not awaiting confirmation';
  end if;

  select proposal.* into v_proposal
  from public.transaction_proposals as proposal
  where proposal.organization_id = v_request.organization_id
    and proposal.id = p_proposal_id
  for update;

  if not found then
    raise exception using errcode = 'P0002', message = 'Replacement proposal was not found';
  end if;
  if v_proposal.created_by <> p_actor_id
     or v_proposal.source_event_id <> v_request.reason_source_event_id
     or v_proposal.status <> 'pending_confirmation' then
    raise exception using
      errcode = '22023',
      message = 'Replacement proposal does not belong to this correction';
  end if;
  if exists (
    select 1
    from public.proposal_lines as line
    where line.organization_id = v_proposal.organization_id
      and line.proposal_id = v_proposal.id
      and (
        line.item_variant_id is null
        or line.base_quantity_delta is null
        or line.base_unit is null
      )
  ) then
    raise exception using
      errcode = '22023',
      message = 'Replacement proposal must contain only resolved catalog variants';
  end if;
  if v_request.replacement_proposal_id = p_proposal_id then
    return p_proposal_id;
  end if;
  if v_request.replacement_proposal_id is not null then
    raise exception using
      errcode = '22023',
      message = 'Reversal request already has a replacement proposal';
  end if;

  update public.transaction_reversal_requests
  set replacement_proposal_id = p_proposal_id,
      updated_at = now()
  where id = p_request_id;

  return p_proposal_id;
end;
$$;

create or replace function public.get_completed_reversal_replacement(
  p_request_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  v_proposal_id uuid;
begin
  select request.replacement_proposal_id into v_proposal_id
  from public.transaction_reversal_requests as request
  join public.transaction_proposals as proposal
    on proposal.organization_id = request.organization_id
   and proposal.id = request.replacement_proposal_id
   and proposal.status = 'pending_confirmation'
  join public.organization_users as member
    on member.organization_id = request.organization_id
   and member.id = p_actor_id
   and member.active
  where request.id = p_request_id
    and request.requested_by = p_actor_id
    and request.status = 'completed';

  return v_proposal_id;
end;
$$;

revoke all on function public.attach_transaction_reversal_replacement(uuid, uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.get_completed_reversal_replacement(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.reset_reversal_replacement_on_reuse()
  from public, anon, authenticated;
revoke all on function public.reject_reversal_replacement_on_cancel()
  from public, anon, authenticated;

grant execute on function public.attach_transaction_reversal_replacement(uuid, uuid, uuid)
  to service_role;
grant execute on function public.get_completed_reversal_replacement(uuid, uuid)
  to service_role;

comment on column public.transaction_reversal_requests.replacement_proposal_id is
  'Grounded corrected proposal shown automatically after this complete reversal succeeds.';
comment on function public.attach_transaction_reversal_replacement(uuid, uuid, uuid) is
  'Links one resolved corrected proposal to an awaiting complete reversal.';
comment on function public.get_completed_reversal_replacement(uuid, uuid) is
  'Returns a linked pending replacement only after its reversal completed.';
