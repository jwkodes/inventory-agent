create or replace function public.prepare_catalog_item_creation_confirmation(
  p_request_id uuid,
  p_actor_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.catalog_item_creation_requests%rowtype;
  v_existing_name text;
  v_existing_attributes jsonb;
  v_message text;
begin
  select request.* into v_request
  from public.catalog_item_creation_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog item request was not found';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1
    from public.organization_users as member
    where member.id = p_actor_id
      and member.organization_id = v_request.organization_id
      and member.active
      and member.role in ('manager', 'admin')
  ) then
    raise exception using errcode = '42501', message = 'Actor cannot confirm this item request';
  end if;
  if v_request.status = 'completed' then
    return jsonb_build_object('ready', true);
  end if;
  if v_request.status = 'awaiting_details'
     and v_request.details_reason is not null then
    return jsonb_build_object(
      'ready', false,
      'request_id', v_request.id,
      'message', v_request.details_reason
    );
  end if;
  if v_request.status <> 'awaiting_confirmation' then
    raise exception using errcode = '22023', message = 'Catalog item request is not ready';
  end if;

  select
    coalesce(variant.name, item.name),
    variant.attributes
  into v_existing_name, v_existing_attributes
  from public.item_variants as variant
  join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
  where variant.organization_id = v_request.organization_id
    and lower(variant.sku) = lower(trim(v_request.sku))
  limit 1;

  if found then
    v_message := format(
      'SKU %s is already used by %s with attributes %s. '
      'The requested new variant has attributes %s. '
      'Reply with a different SKU, or cancel and choose the existing item if they are the same.',
      v_request.sku,
      v_existing_name,
      coalesce(v_existing_attributes, '{}'::jsonb)::text,
      coalesce(v_request.attributes, '{}'::jsonb)::text
    );
    update public.catalog_item_creation_requests
    set status = 'awaiting_details',
        sku = null,
        suggested_sku = null,
        details_reason = v_message,
        updated_at = now()
    where id = v_request.id;
    return jsonb_build_object(
      'ready', false,
      'request_id', v_request.id,
      'message', v_message
    );
  end if;

  return jsonb_build_object('ready', true);
end;
$$;

comment on function public.prepare_catalog_item_creation_confirmation(uuid, uuid) is
  'Reopens duplicate-SKU drafts and idempotently repeats their correction prompt.';
