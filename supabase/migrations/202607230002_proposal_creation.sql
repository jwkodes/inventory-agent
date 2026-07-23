create or replace function public.create_inventory_proposal(
  p_organization_id uuid,
  p_location_id uuid,
  p_source_event_id uuid,
  p_created_by uuid,
  p_intent public.proposal_intent,
  p_idempotency_key text,
  p_raw_command jsonb,
  p_model_name text,
  p_model_response_id text,
  p_prompt_version text,
  p_notes text,
  p_lines jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_proposal_id uuid;
  v_line record;
  v_item record;
  v_factor numeric(24, 8);
  v_base_delta numeric(24, 8);
  v_base_unit text;
begin
  if p_intent = 'adjust_stock' then
    raise exception using
      errcode = '0A000',
      message = 'Adjustment proposals require an explicit adjustment mode and are not yet supported';
  end if;

  if p_idempotency_key is null or length(trim(p_idempotency_key)) = 0 then
    raise exception using errcode = '22023', message = 'An idempotency key is required';
  end if;

  if jsonb_typeof(p_lines) <> 'array' or jsonb_array_length(p_lines) = 0 then
    raise exception using errcode = '22023', message = 'A proposal requires at least one line';
  end if;

  if not exists (
    select 1 from public.organization_users as member
    where member.organization_id = p_organization_id
      and member.id = p_created_by
      and member.active
  ) then
    raise exception using errcode = '42501', message = 'Creator is not an active organization member';
  end if;

  if not exists (
    select 1 from public.locations as location
    where location.organization_id = p_organization_id
      and location.id = p_location_id
      and location.active
  ) then
    raise exception using errcode = '22023', message = 'Location is not active in the organization';
  end if;

  insert into public.transaction_proposals (
    organization_id,
    location_id,
    source_event_id,
    created_by,
    intent,
    idempotency_key,
    raw_command,
    model_name,
    model_response_id,
    prompt_version,
    notes
  )
  values (
    p_organization_id,
    p_location_id,
    p_source_event_id,
    p_created_by,
    p_intent,
    trim(p_idempotency_key),
    coalesce(p_raw_command, '{}'::jsonb),
    p_model_name,
    p_model_response_id,
    p_prompt_version,
    p_notes
  )
  on conflict (organization_id, idempotency_key) do nothing
  returning id into v_proposal_id;

  if v_proposal_id is null then
    select proposal.id into v_proposal_id
    from public.transaction_proposals as proposal
    where proposal.organization_id = p_organization_id
      and proposal.idempotency_key = trim(p_idempotency_key);
    return v_proposal_id;
  end if;

  for v_line in
    select *
    from jsonb_to_recordset(p_lines) as line (
      line_number integer,
      source_text text,
      extracted_description text,
      requested_quantity numeric(24, 8),
      requested_unit text,
      item_variant_id uuid,
      lot_id uuid,
      serial_id uuid,
      match_method public.match_method,
      match_score numeric(8, 7),
      match_evidence jsonb,
      attributes jsonb
    )
    order by line_number
  loop
    if v_line.line_number is null or v_line.line_number <= 0
       or v_line.source_text is null
       or v_line.requested_quantity is null
       or v_line.requested_quantity <= 0 then
      raise exception using errcode = '22023', message = 'Proposal line is incomplete';
    end if;

    v_factor := null;
    v_base_delta := null;
    v_base_unit := null;

    if v_line.item_variant_id is not null then
      select item.base_unit, item.tracking_mode
      into v_item
      from public.item_variants as variant
      join public.items as item
        on item.organization_id = variant.organization_id
       and item.id = variant.item_id
      where variant.organization_id = p_organization_id
        and variant.id = v_line.item_variant_id
        and variant.active
        and item.active;

      if not found then
        raise exception using errcode = '22023', message = 'Resolved variant is not active in the organization';
      end if;

      if v_line.requested_unit is null
         or lower(trim(v_line.requested_unit)) = lower(v_item.base_unit) then
        v_factor := 1;
      else
        select conversion.factor_to_base into v_factor
        from public.item_unit_conversions as conversion
        where conversion.organization_id = p_organization_id
          and conversion.item_variant_id = v_line.item_variant_id
          and lower(conversion.from_unit) = lower(trim(v_line.requested_unit));
      end if;

      if v_factor is null then
        raise exception using
          errcode = '22023',
          message = format('No unit conversion exists for proposal line %s', v_line.line_number);
      end if;

      v_base_delta := v_line.requested_quantity * v_factor
        * case when p_intent = 'issue_stock' then -1 else 1 end;
      v_base_unit := v_item.base_unit;
    end if;

    insert into public.proposal_lines (
      organization_id,
      proposal_id,
      line_number,
      source_text,
      extracted_description,
      requested_quantity,
      requested_unit,
      item_variant_id,
      lot_id,
      serial_id,
      base_quantity_delta,
      base_unit,
      match_method,
      match_score,
      match_evidence,
      attributes
    )
    values (
      p_organization_id,
      v_proposal_id,
      v_line.line_number,
      v_line.source_text,
      v_line.extracted_description,
      v_line.requested_quantity,
      v_line.requested_unit,
      v_line.item_variant_id,
      v_line.lot_id,
      v_line.serial_id,
      v_base_delta,
      v_base_unit,
      v_line.match_method,
      v_line.match_score,
      coalesce(v_line.match_evidence, '{}'::jsonb),
      coalesce(v_line.attributes, '{}'::jsonb)
    );
  end loop;

  return v_proposal_id;
end;
$$;

revoke all on function public.create_inventory_proposal(
  uuid, uuid, uuid, uuid, public.proposal_intent, text, jsonb, text, text, text, text, jsonb
) from public, anon, authenticated;
grant execute on function public.create_inventory_proposal(
  uuid, uuid, uuid, uuid, public.proposal_intent, text, jsonb, text, text, text, text, jsonb
) to service_role;

comment on function public.create_inventory_proposal(
  uuid, uuid, uuid, uuid, public.proposal_intent, text, jsonb, text, text, text, text, jsonb
) is 'Idempotently creates a proposal and derives base-unit deltas for resolved lines.';
