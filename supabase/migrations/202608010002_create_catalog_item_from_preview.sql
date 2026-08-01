create or replace function public.create_catalog_item_from_agent_preview(
  p_proposal_line_id uuid,
  p_actor_id uuid,
  p_chat_id bigint
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.catalog_item_creation_requests%rowtype;
  v_request_id uuid;
  v_proposal_id uuid;
  v_preparation jsonb;
begin
  select request.* into v_request
  from public.catalog_item_creation_requests as request
  where request.proposal_line_id = p_proposal_line_id
  for update;

  if found and v_request.status = 'completed' then
    v_proposal_id := public.confirm_catalog_item_creation(v_request.id, p_actor_id);
    return jsonb_build_object(
      'status', 'completed',
      'result_id', v_proposal_id
    );
  end if;

  perform public.mark_proposal_line_as_new_item(p_proposal_line_id, p_actor_id);
  v_request_id := public.begin_catalog_item_creation(
    p_proposal_line_id,
    p_actor_id,
    p_chat_id
  );

  select request.* into strict v_request
  from public.catalog_item_creation_requests as request
  where request.id = v_request_id
  for update;

  if v_request.status = 'awaiting_details' then
    return jsonb_strip_nulls(jsonb_build_object(
      'status', 'awaiting_details',
      'result_id', v_request.id,
      'message', v_request.details_reason
    ));
  end if;

  if v_request.status = 'completed' then
    v_proposal_id := public.confirm_catalog_item_creation(v_request.id, p_actor_id);
    return jsonb_build_object(
      'status', 'completed',
      'result_id', v_proposal_id
    );
  end if;

  if v_request.status <> 'awaiting_confirmation' then
    raise exception using
      errcode = '22023',
      message = 'Catalog item preview is not ready to create';
  end if;

  v_preparation := public.prepare_catalog_item_creation_confirmation(
    v_request.id,
    p_actor_id
  );
  if coalesce((v_preparation ->> 'ready')::boolean, false) = false then
    return jsonb_strip_nulls(jsonb_build_object(
      'status', 'awaiting_details',
      'result_id', v_request.id,
      'message', v_preparation ->> 'message'
    ));
  end if;

  v_proposal_id := public.confirm_catalog_item_creation(v_request.id, p_actor_id);
  return jsonb_build_object(
    'status', 'completed',
    'result_id', v_proposal_id
  );
end;
$$;

revoke all on function public.create_catalog_item_from_agent_preview(uuid, uuid, bigint)
  from public, anon, authenticated;
grant execute on function public.create_catalog_item_from_agent_preview(uuid, uuid, bigint)
  to service_role;

comment on function public.create_catalog_item_from_agent_preview(uuid, uuid, bigint) is
  'Atomically accepts a complete agent catalog preview and safely resumes on retries.';
