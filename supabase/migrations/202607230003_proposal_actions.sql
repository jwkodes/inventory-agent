create or replace function public.resolve_proposal_line(
  p_proposal_line_id uuid,
  p_item_variant_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_line public.proposal_lines%rowtype;
  v_proposal public.transaction_proposals%rowtype;
  v_item record;
  v_factor numeric(24, 8);
begin
  select line.* into v_line from public.proposal_lines as line
  where line.id = p_proposal_line_id for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Proposal line was not found';
  end if;

  select proposal.* into v_proposal from public.transaction_proposals as proposal
  where proposal.id = v_line.proposal_id for update;
  if v_proposal.status <> 'pending_confirmation' then
    raise exception using errcode = '22023', message = 'Proposal is no longer pending';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_line.organization_id
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor is not an active organization member';
  end if;
  if not exists (
    select 1 from jsonb_array_elements(coalesce(v_line.match_evidence -> 'candidates', '[]')) as candidate
    where candidate ->> 'item_variant_id' = p_item_variant_id::text
  ) then
    raise exception using errcode = '22023', message = 'Selected variant was not offered for this line';
  end if;

  select item.base_unit, item.tracking_mode into v_item
  from public.item_variants as variant
  join public.items as item
    on item.organization_id = variant.organization_id and item.id = variant.item_id
  where variant.organization_id = v_line.organization_id
    and variant.id = p_item_variant_id and variant.active and item.active;
  if not found then
    raise exception using errcode = '22023', message = 'Selected variant is not active';
  end if;
  if v_item.tracking_mode <> 'simple' then
    raise exception using errcode = '0A000', message = 'Lot or serial details are required for this variant';
  end if;

  if v_line.requested_unit is null or lower(trim(v_line.requested_unit)) = lower(v_item.base_unit) then
    v_factor := 1;
  else
    select conversion.factor_to_base into v_factor
    from public.item_unit_conversions as conversion
    where conversion.organization_id = v_line.organization_id
      and conversion.item_variant_id = p_item_variant_id
      and lower(conversion.from_unit) = lower(trim(v_line.requested_unit));
  end if;
  if v_factor is null then
    raise exception using errcode = '22023', message = 'No unit conversion exists for selected variant';
  end if;

  update public.proposal_lines
  set item_variant_id = p_item_variant_id,
      base_quantity_delta = v_line.requested_quantity * v_factor
        * case when v_proposal.intent = 'issue_stock' then -1 else 1 end,
      base_unit = v_item.base_unit,
      match_method = 'human_selected',
      match_score = 1,
      match_evidence = v_line.match_evidence || jsonb_build_object(
        'selected_item_variant_id', p_item_variant_id,
        'selected_by', p_actor_id,
        'selected_at', now()
      )
  where id = p_proposal_line_id;
  return v_line.proposal_id;
end;
$$;

create or replace function public.cancel_inventory_proposal(p_proposal_id uuid, p_actor_id uuid)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_proposal public.transaction_proposals%rowtype;
begin
  select proposal.* into v_proposal from public.transaction_proposals as proposal
  where proposal.id = p_proposal_id for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Proposal was not found';
  end if;
  if not exists (
    select 1 from public.organization_users as member
    where member.id = p_actor_id and member.organization_id = v_proposal.organization_id and member.active
  ) then
    raise exception using errcode = '42501', message = 'Actor is not an active organization member';
  end if;
  if v_proposal.status = 'rejected' then return v_proposal.id; end if;
  if v_proposal.status <> 'pending_confirmation' then
    raise exception using errcode = '22023', message = 'Only pending proposals can be cancelled';
  end if;
  update public.transaction_proposals set status = 'rejected' where id = p_proposal_id;
  return p_proposal_id;
end;
$$;

revoke all on function public.resolve_proposal_line(uuid, uuid, uuid) from public, anon, authenticated;
revoke all on function public.cancel_inventory_proposal(uuid, uuid) from public, anon, authenticated;
grant execute on function public.resolve_proposal_line(uuid, uuid, uuid) to service_role;
grant execute on function public.cancel_inventory_proposal(uuid, uuid) to service_role;
