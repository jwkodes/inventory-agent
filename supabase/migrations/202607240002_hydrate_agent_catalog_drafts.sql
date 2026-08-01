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
begin
  if new.status <> 'awaiting_details' then
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

drop trigger if exists catalog_item_creation_agent_draft
  on public.catalog_item_creation_requests;
create trigger catalog_item_creation_agent_draft
before insert or update of proposal_line_id, status
on public.catalog_item_creation_requests
for each row execute function public.hydrate_catalog_item_creation_from_agent_draft();

revoke all on function public.hydrate_catalog_item_creation_from_agent_draft()
  from public, anon, authenticated;
grant execute on function public.hydrate_catalog_item_creation_from_agent_draft()
  to service_role;

comment on function public.hydrate_catalog_item_creation_from_agent_draft() is
  'Carries user-supplied agent catalog facts and optional attributes into item creation.';
