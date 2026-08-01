alter function public.reset_organization_inventory_data(uuid, uuid, text)
  rename to reset_organization_inventory_data_internal;

revoke all on function public.reset_organization_inventory_data_internal(uuid, uuid, text)
  from public, anon, authenticated, service_role;

create or replace function public.reset_organization_inventory_data(
  p_organization_id uuid,
  p_actor_id uuid,
  p_confirmation text
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_slug text;
  v_expected_confirmation text;
begin
  select organization.slug
  into v_slug
  from public.organizations as organization
  where organization.id = p_organization_id;

  if v_slug is null then
    raise exception using
      errcode = '22023',
      message = 'The selected organization does not exist';
  end if;

  v_expected_confirmation := format('RESET %s', v_slug);
  if p_confirmation is distinct from v_expected_confirmation then
    raise exception using
      errcode = '22023',
      message = format(
        'Type %s exactly to confirm the inventory data reset',
        v_expected_confirmation
      );
  end if;

  return public.reset_organization_inventory_data_internal(
    p_organization_id,
    p_actor_id,
    'RESET'
  );
end;
$$;

revoke all on function public.reset_organization_inventory_data(uuid, uuid, text)
  from public, anon, authenticated;
grant execute on function public.reset_organization_inventory_data(uuid, uuid, text)
  to service_role;

comment on function public.reset_organization_inventory_data(uuid, uuid, text) is
  'Validates a company-specific typed acknowledgement, then atomically clears that company operational test data.';
