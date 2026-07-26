create or replace function public.cancel_command_clarification(
  p_request_id uuid,
  p_event_id uuid,
  p_actor_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_request public.command_clarification_requests%rowtype;
begin
  select request.* into v_request
  from public.command_clarification_requests as request
  where request.id = p_request_id
  for update;
  if not found then
    raise exception using
      errcode = 'P0002',
      message = 'Command clarification was not found';
  end if;
  if v_request.status = 'cancelled'
     and v_request.last_source_event_id = p_event_id then
    return v_request.id;
  end if;
  if v_request.status <> 'awaiting_reply' then
    raise exception using
      errcode = '22023',
      message = 'Command clarification is not awaiting a reply';
  end if;
  if v_request.requested_by <> p_actor_id or not exists (
    select 1
    from public.source_events as source_event
    where source_event.organization_id = v_request.organization_id
      and source_event.id = p_event_id
  ) then
    raise exception using
      errcode = '42501',
      message = 'Actor cannot cancel command clarification';
  end if;

  update public.command_clarification_requests
  set status = 'cancelled',
      last_source_event_id = p_event_id,
      updated_at = now(),
      resolved_at = now()
  where id = v_request.id;
  return v_request.id;
end;
$$;

revoke all on function public.cancel_command_clarification(uuid, uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.cancel_command_clarification(uuid, uuid, uuid)
  to service_role;

comment on function public.cancel_command_clarification(uuid, uuid, uuid) is
  'Abandons an actor-scoped pending command clarification so later chat is not intercepted.';
