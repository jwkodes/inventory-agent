create table public.organization_data_resets (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  requested_by uuid not null,
  reset_scope text not null,
  deleted_counts jsonb not null,
  created_at timestamptz not null default now(),
  foreign key (organization_id, requested_by)
    references public.organization_users (organization_id, id),
  check (reset_scope = 'operational_inventory'),
  check (jsonb_typeof(deleted_counts) = 'object')
);

create index organization_data_resets_org_created_idx
  on public.organization_data_resets (organization_id, created_at desc);

alter table public.organization_data_resets enable row level security;
grant select, insert on public.organization_data_resets to service_role;

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
  v_counts jsonb;
  v_reset_id uuid;
begin
  if p_confirmation <> 'RESET' then
    raise exception using
      errcode = '22023',
      message = 'Type RESET exactly to confirm the inventory data reset';
  end if;
  if not exists (
    select 1
    from public.organization_users as member
    where member.organization_id = p_organization_id
      and member.id = p_actor_id
      and member.active
      and member.role = 'admin'
  ) then
    raise exception using
      errcode = '42501',
      message = 'Only an active organization admin can reset inventory data';
  end if;

  perform pg_advisory_xact_lock(hashtextextended(p_organization_id::text, 0));

  select jsonb_build_object(
    'items', (
      select count(*) from public.items where organization_id = p_organization_id
    ),
    'variants', (
      select count(*) from public.item_variants where organization_id = p_organization_id
    ),
    'balances', (
      select count(*) from public.inventory_balances
      where organization_id = p_organization_id
    ),
    'transactions', (
      select count(*) from public.inventory_transactions
      where organization_id = p_organization_id
    ),
    'proposals', (
      select count(*) from public.transaction_proposals
      where organization_id = p_organization_id
    ),
    'source_events', (
      select count(*) from public.source_events
      where organization_id = p_organization_id
    ),
    'source_artifacts', (
      select count(*) from public.source_artifacts
      where organization_id = p_organization_id
    ),
    'agent_conversations', (
      select count(*) from public.inventory_agent_conversations
      where organization_id = p_organization_id
    )
  ) into v_counts;

  delete from public.inventory_agent_conversations
  where organization_id = p_organization_id;

  delete from public.processing_outbox
  where organization_id = p_organization_id;
  delete from public.catalog_item_creation_requests
  where organization_id = p_organization_id;
  delete from public.match_clarification_requests
  where organization_id = p_organization_id;
  delete from public.transaction_reversal_requests
  where organization_id = p_organization_id;

  update public.transaction_proposals
  set applied_transaction_id = null
  where organization_id = p_organization_id
    and applied_transaction_id is not null;

  delete from public.stock_movements
  where organization_id = p_organization_id;
  delete from public.transaction_lines
  where organization_id = p_organization_id;
  delete from public.inventory_transactions
  where organization_id = p_organization_id;
  delete from public.proposal_lines
  where organization_id = p_organization_id;
  delete from public.transaction_proposals
  where organization_id = p_organization_id;

  delete from public.source_artifacts
  where organization_id = p_organization_id;
  delete from public.source_events
  where organization_id = p_organization_id;

  delete from public.inventory_balances
  where organization_id = p_organization_id;
  delete from public.inventory_lots
  where organization_id = p_organization_id;
  delete from public.inventory_serials
  where organization_id = p_organization_id;
  delete from public.inventory_variant_embeddings
  where organization_id = p_organization_id;
  delete from public.item_aliases
  where organization_id = p_organization_id;
  delete from public.item_identifiers
  where organization_id = p_organization_id;
  delete from public.item_unit_conversions
  where organization_id = p_organization_id;
  delete from public.item_variants
  where organization_id = p_organization_id;
  delete from public.items
  where organization_id = p_organization_id;

  insert into public.organization_data_resets (
    organization_id,
    requested_by,
    reset_scope,
    deleted_counts
  )
  values (
    p_organization_id,
    p_actor_id,
    'operational_inventory',
    v_counts
  )
  returning id into v_reset_id;

  return jsonb_build_object(
    'status', 'reset',
    'reset_id', v_reset_id,
    'organization_id', p_organization_id,
    'deleted_counts', v_counts,
    'preserved', jsonb_build_array(
      'organization',
      'members',
      'roles',
      'locations',
      'custom_fields',
      'organization_settings',
      'registration'
    )
  );
end;
$$;

revoke all on function public.reset_organization_inventory_data(uuid, uuid, text)
  from public, anon, authenticated;
grant execute on function public.reset_organization_inventory_data(uuid, uuid, text)
  to service_role;

comment on function public.reset_organization_inventory_data(uuid, uuid, text) is
  'Atomically clears one organization operational inventory and conversation data for development stress tests.';
