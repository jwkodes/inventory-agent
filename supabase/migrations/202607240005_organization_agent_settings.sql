create table public.organization_setting_changes (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  setting_key text not null,
  old_value jsonb,
  new_value jsonb,
  changed_by text not null,
  created_at timestamptz not null default now(),
  check (length(setting_key) between 1 and 120),
  check (length(changed_by) between 1 and 200)
);

create index organization_setting_changes_organization_created_idx
  on public.organization_setting_changes (organization_id, created_at desc);

alter table public.organization_setting_changes enable row level security;
grant select, insert on public.organization_setting_changes to service_role;

create or replace function public.load_organization_agent_context_settings(
  p_organization_id uuid
)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  v_settings jsonb;
begin
  select organization.settings #> '{inventory_agent,context}'
  into v_settings
  from public.organizations as organization
  where organization.id = p_organization_id;

  if not found then
    raise exception using errcode = 'P0002', message = 'Organization was not found';
  end if;

  return v_settings;
end;
$$;

create or replace function public.set_organization_agent_context_settings(
  p_organization_id uuid,
  p_policy text,
  p_retention_days integer,
  p_max_tokens integer,
  p_max_items integer,
  p_changed_by text
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_settings jsonb;
  v_old jsonb;
  v_new jsonb;
begin
  if p_policy not in ('discard', 'summarize') then
    raise exception using errcode = '22023', message = 'Context policy must be discard or summarize';
  end if;
  if p_retention_days is null or p_retention_days < 1 then
    raise exception using errcode = '22023', message = 'Context retention days must be positive';
  end if;
  if p_max_tokens is null or p_max_tokens < 1 then
    raise exception using errcode = '22023', message = 'Context token limit must be positive';
  end if;
  if p_max_items is null or p_max_items < 1 or p_max_items > 350 then
    raise exception using errcode = '22023', message = 'Context item limit must be between 1 and 350';
  end if;
  if p_changed_by is null or length(trim(p_changed_by)) not between 1 and 200 then
    raise exception using errcode = '22023', message = 'Settings change actor is required';
  end if;

  select organization.settings
  into v_settings
  from public.organizations as organization
  where organization.id = p_organization_id
  for update;

  if not found then
    raise exception using errcode = 'P0002', message = 'Organization was not found';
  end if;

  v_old := v_settings #> '{inventory_agent,context}';
  v_new := jsonb_build_object(
    'policy', p_policy,
    'retention_days', p_retention_days,
    'max_tokens', p_max_tokens,
    'max_items', p_max_items
  );

  update public.organizations
  set settings = v_settings || jsonb_build_object(
    'inventory_agent',
    coalesce(v_settings -> 'inventory_agent', '{}'::jsonb)
      || jsonb_build_object('context', v_new)
  )
  where id = p_organization_id;

  insert into public.organization_setting_changes (
    organization_id,
    setting_key,
    old_value,
    new_value,
    changed_by
  )
  values (
    p_organization_id,
    'inventory_agent.context',
    v_old,
    v_new,
    trim(p_changed_by)
  );

  return v_new;
end;
$$;

create or replace function public.clear_organization_agent_context_settings(
  p_organization_id uuid,
  p_changed_by text
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_settings jsonb;
  v_old jsonb;
begin
  if p_changed_by is null or length(trim(p_changed_by)) not between 1 and 200 then
    raise exception using errcode = '22023', message = 'Settings change actor is required';
  end if;

  select organization.settings
  into v_settings
  from public.organizations as organization
  where organization.id = p_organization_id
  for update;

  if not found then
    raise exception using errcode = 'P0002', message = 'Organization was not found';
  end if;

  v_old := v_settings #> '{inventory_agent,context}';
  update public.organizations
  set settings = v_settings #- '{inventory_agent,context}'
  where id = p_organization_id;

  if v_old is not null then
    insert into public.organization_setting_changes (
      organization_id,
      setting_key,
      old_value,
      new_value,
      changed_by
    )
    values (
      p_organization_id,
      'inventory_agent.context',
      v_old,
      null,
      trim(p_changed_by)
    );
  end if;

  return v_old;
end;
$$;

revoke all on function public.load_organization_agent_context_settings(uuid)
  from public, anon, authenticated;
revoke all on function public.set_organization_agent_context_settings(
  uuid, text, integer, integer, integer, text
) from public, anon, authenticated;
revoke all on function public.clear_organization_agent_context_settings(uuid, text)
  from public, anon, authenticated;

grant execute on function public.load_organization_agent_context_settings(uuid)
  to service_role;
grant execute on function public.set_organization_agent_context_settings(
  uuid, text, integer, integer, integer, text
) to service_role;
grant execute on function public.clear_organization_agent_context_settings(uuid, text)
  to service_role;

comment on table public.organization_setting_changes is
  'Immutable audit trail for dashboard-managed, non-secret organization settings.';
comment on function public.load_organization_agent_context_settings(uuid) is
  'Returns a complete per-organization context override or null for application defaults.';
