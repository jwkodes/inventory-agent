create or replace function public.confirm_catalog_batch_and_apply_inventory(
  p_batch_id uuid,
  p_actor_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_catalog_result jsonb;
  v_proposal_id uuid;
  v_transaction_id uuid;
begin
  v_catalog_result := public.confirm_catalog_batch_creation(
    p_batch_id,
    p_actor_id
  );
  if coalesce((v_catalog_result ->> 'ready')::boolean, false) = false then
    return v_catalog_result;
  end if;

  v_proposal_id := (v_catalog_result ->> 'proposal_id')::uuid;
  v_transaction_id := public.apply_inventory_proposal(
    v_proposal_id,
    p_actor_id
  );
  return jsonb_build_object(
    'ready', true,
    'proposal_id', v_proposal_id,
    'transaction_id', v_transaction_id
  );
end;
$$;

create or replace function public.cancel_catalog_batch_and_proposal(
  p_batch_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_batch public.catalog_batch_creation_requests%rowtype;
begin
  select batch.* into v_batch
  from public.catalog_batch_creation_requests as batch
  where batch.id = p_batch_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog batch was not found';
  end if;

  perform public.cancel_catalog_batch_creation(p_batch_id, p_actor_id);
  perform public.cancel_inventory_proposal(v_batch.proposal_id, p_actor_id);
  return v_batch.id;
end;
$$;

revoke all on function public.confirm_catalog_batch_and_apply_inventory(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.cancel_catalog_batch_and_proposal(uuid, uuid)
  from public, anon, authenticated;

grant execute on function public.confirm_catalog_batch_and_apply_inventory(uuid, uuid)
  to service_role;
grant execute on function public.cancel_catalog_batch_and_proposal(uuid, uuid)
  to service_role;

comment on function public.confirm_catalog_batch_and_apply_inventory(uuid, uuid) is
  'Atomically creates a selected catalog batch and applies its complete stock receipt.';
comment on function public.cancel_catalog_batch_and_proposal(uuid, uuid) is
  'Cancels the catalog batch and its stock proposal together without changing inventory.';
