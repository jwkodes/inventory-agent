create or replace function public.hydrate_catalog_item_creation_from_agent_draft()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_draft jsonb;
  v_attributes jsonb := '{}'::jsonb;
  v_source_event_id uuid;
  v_tracking_mode text;
  v_existing_name text;
  v_existing_attributes jsonb;
begin
  if new.status <> 'awaiting_details' then
    return new;
  end if;
  if new.details_reason is not null and new.sku is null then
    return new;
  end if;

  select
    line.match_evidence -> 'new_item',
    proposal.source_event_id
  into v_draft, v_source_event_id
  from public.proposal_lines as line
  join public.transaction_proposals as proposal
    on proposal.id = line.proposal_id
  where line.id = new.proposal_line_id;

  if jsonb_typeof(v_draft) <> 'object' then
    return new;
  end if;

  if jsonb_typeof(v_draft -> 'attributes') = 'array' then
    select coalesce(
      jsonb_object_agg(trim(attribute ->> 'key'), attribute ->> 'value'),
      '{}'::jsonb
    )
    into v_attributes
    from jsonb_array_elements(v_draft -> 'attributes') as attribute
    where nullif(trim(attribute ->> 'key'), '') is not null
      and nullif(trim(attribute ->> 'value'), '') is not null;
  end if;

  v_tracking_mode := nullif(lower(trim(v_draft ->> 'tracking_mode')), '');
  if v_tracking_mode not in ('simple', 'lot', 'serial') then
    v_tracking_mode := null;
  end if;

  new.name := coalesce(nullif(trim(v_draft ->> 'name'), ''), new.name);
  new.sku := coalesce(nullif(trim(v_draft ->> 'sku'), ''), new.sku);
  new.base_unit := coalesce(
    nullif(lower(trim(v_draft ->> 'base_unit')), ''),
    new.base_unit
  );
  new.tracking_mode := coalesce(
    v_tracking_mode::public.tracking_mode,
    new.tracking_mode
  );
  new.attributes := coalesce(new.attributes, '{}'::jsonb) || v_attributes;
  new.suggested_name := coalesce(new.name, new.suggested_name);
  new.suggested_sku := coalesce(new.sku, new.suggested_sku);
  new.suggested_base_unit := coalesce(new.base_unit, new.suggested_base_unit);
  new.suggested_tracking_mode := coalesce(
    new.tracking_mode,
    new.suggested_tracking_mode
  );
  new.details_reason := null;

  if new.sku is not null then
    select
      coalesce(variant.name, item.name),
      variant.attributes
    into v_existing_name, v_existing_attributes
    from public.item_variants as variant
    join public.items as item
      on item.organization_id = variant.organization_id
     and item.id = variant.item_id
    where variant.organization_id = new.organization_id
      and lower(variant.sku) = lower(trim(new.sku))
    limit 1;

    if found then
      new.details_reason := format(
        'SKU %s is already used by %s with attributes %s. '
        'The requested new variant has attributes %s. '
        'Reply with a different SKU, or cancel and choose the existing item if they are the same.',
        new.sku,
        v_existing_name,
        coalesce(v_existing_attributes, '{}'::jsonb)::text,
        coalesce(new.attributes, '{}'::jsonb)::text
      );
      new.sku := null;
      new.suggested_sku := null;
      new.status := 'awaiting_details';
      return new;
    end if;
  end if;

  if new.name is not null
    and new.sku is not null
    and new.base_unit is not null
    and new.tracking_mode = 'simple'
  then
    new.status := 'awaiting_confirmation';
    new.details_source_event_id := v_source_event_id;
  end if;
  return new;
end;
$$;

comment on function public.hydrate_catalog_item_creation_from_agent_draft() is
  'Hydrates agent drafts while preserving a duplicate-SKU request reopened for correction.';
