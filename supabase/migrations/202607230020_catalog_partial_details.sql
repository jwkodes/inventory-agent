create or replace function public.save_catalog_item_creation_draft(
  p_request_id uuid,
  p_event_id uuid,
  p_actor_id uuid,
  p_name text,
  p_sku text,
  p_base_unit text,
  p_tracking_mode text,
  p_attributes jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.catalog_item_creation_requests%rowtype;
begin
  select request.* into v_request
  from public.catalog_item_creation_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Catalog request was not found';
  end if;
  if v_request.status <> 'awaiting_details' then
    raise exception using errcode = '22023', message = 'Catalog request is not awaiting details';
  end if;
  if v_request.requested_by <> p_actor_id then
    raise exception using errcode = '42501', message = 'Catalog request belongs to another user';
  end if;
  if not exists (
    select 1
    from public.source_events as event
    where event.id = p_event_id
      and event.organization_id = v_request.organization_id
      and event.status = 'processing'
  ) then
    raise exception using errcode = '22023', message = 'Detail source event is invalid';
  end if;
  if p_tracking_mode is not null
    and p_tracking_mode not in ('simple', 'lot', 'serial') then
    raise exception using errcode = '22023', message = 'Tracking mode is invalid';
  end if;
  if p_attributes is not null and jsonb_typeof(p_attributes) <> 'object' then
    raise exception using errcode = '22023', message = 'Attributes must be an object';
  end if;

  update public.catalog_item_creation_requests
  set name = coalesce(nullif(trim(p_name), ''), name),
      sku = coalesce(nullif(trim(p_sku), ''), sku),
      base_unit = coalesce(nullif(lower(trim(p_base_unit)), ''), base_unit),
      tracking_mode = coalesce(
        nullif(lower(trim(p_tracking_mode)), '')::public.tracking_mode,
        tracking_mode
      ),
      attributes = attributes || coalesce(p_attributes, '{}'::jsonb),
      details_source_event_id = p_event_id,
      updated_at = now()
  where id = p_request_id;
  return p_request_id;
end;
$$;

revoke all on function public.save_catalog_item_creation_draft(
  uuid, uuid, uuid, text, text, text, text, jsonb
) from public, anon, authenticated;
grant execute on function public.save_catalog_item_creation_draft(
  uuid, uuid, uuid, text, text, text, text, jsonb
) to service_role;

comment on function public.save_catalog_item_creation_draft(
  uuid, uuid, uuid, text, text, text, text, jsonb
) is 'Merges partial natural-language catalog details across clarification turns.';
