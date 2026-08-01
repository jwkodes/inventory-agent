create sequence public.telegram_dev_user_id_seq
  as bigint
  start with 4000000000000000
  increment by 1
  minvalue 4000000000000000
  maxvalue 4499999999999999
  no cycle;

create table public.telegram_dev_personas (
  id uuid primary key default extensions.gen_random_uuid(),
  controller_telegram_user_id bigint not null,
  alias text not null,
  synthetic_telegram_user_id bigint not null
    default nextval('public.telegram_dev_user_id_seq'),
  display_name text not null,
  telegram_username text not null,
  created_at timestamptz not null default now(),
  last_used_at timestamptz not null default now(),
  unique (controller_telegram_user_id, alias),
  unique (synthetic_telegram_user_id),
  check (controller_telegram_user_id > 0),
  check (synthetic_telegram_user_id between 4000000000000000 and 4499999999999999),
  check (alias ~ '^[a-z][a-z0-9_-]{0,27}$'),
  check (length(trim(display_name)) between 1 and 100),
  check (telegram_username ~ '^dev_[a-z][a-z0-9_]{0,27}$')
);

create table public.telegram_dev_persona_sessions (
  controller_telegram_user_id bigint not null,
  chat_id bigint not null,
  persona_id uuid not null references public.telegram_dev_personas (id) on delete cascade,
  expires_at timestamptz not null,
  updated_at timestamptz not null default now(),
  primary key (controller_telegram_user_id, chat_id),
  check (controller_telegram_user_id > 0),
  check (chat_id <> 0)
);

create index telegram_dev_personas_synthetic_idx
  on public.telegram_dev_personas (synthetic_telegram_user_id);
create index telegram_dev_persona_sessions_expiry_idx
  on public.telegram_dev_persona_sessions (expires_at);

alter table public.telegram_dev_personas enable row level security;
alter table public.telegram_dev_persona_sessions enable row level security;

grant select, insert, update, delete on public.telegram_dev_personas to service_role;
grant select, insert, update, delete on public.telegram_dev_persona_sessions to service_role;
grant usage, select on sequence public.telegram_dev_user_id_seq to service_role;

create or replace function public.telegram_dev_controller_is_admin(
  p_controller_telegram_user_id bigint
)
returns boolean
language sql
security definer
stable
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.organization_users as member
    where member.telegram_user_id = p_controller_telegram_user_id
      and member.active
      and member.role = 'admin'
  );
$$;

create or replace function public.activate_telegram_dev_persona(
  p_controller_telegram_user_id bigint,
  p_chat_id bigint,
  p_alias text,
  p_display_name text,
  p_session_minutes integer
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_alias text := lower(trim(p_alias));
  v_persona public.telegram_dev_personas%rowtype;
  v_expires_at timestamptz;
  v_organization_name text;
  v_role public.organization_role;
begin
  if not public.telegram_dev_controller_is_admin(p_controller_telegram_user_id) then
    raise exception using
      errcode = '42501',
      message = 'Only an active organization admin can simulate Telegram users';
  end if;
  if p_chat_id = 0 then
    raise exception using errcode = '22023', message = 'Telegram chat ID is invalid';
  end if;
  if v_alias !~ '^[a-z][a-z0-9_-]{0,27}$' or v_alias = 'me' then
    raise exception using
      errcode = '22023',
      message = 'Persona alias must start with a letter and use only letters, numbers, underscores, or hyphens';
  end if;
  if nullif(trim(p_display_name), '') is null then
    raise exception using errcode = '22023', message = 'Persona display name is required';
  end if;
  if p_session_minutes < 5 or p_session_minutes > 1440 then
    raise exception using
      errcode = '22023',
      message = 'Persona session must be between 5 and 1440 minutes';
  end if;

  insert into public.telegram_dev_personas (
    controller_telegram_user_id,
    alias,
    display_name,
    telegram_username
  )
  values (
    p_controller_telegram_user_id,
    v_alias,
    trim(p_display_name),
    'dev_' || replace(v_alias, '-', '_')
  )
  on conflict (controller_telegram_user_id, alias)
  do update set
    display_name = excluded.display_name,
    last_used_at = now()
  returning * into v_persona;

  v_expires_at := now() + make_interval(mins => p_session_minutes);
  insert into public.telegram_dev_persona_sessions (
    controller_telegram_user_id,
    chat_id,
    persona_id,
    expires_at
  )
  values (
    p_controller_telegram_user_id,
    p_chat_id,
    v_persona.id,
    v_expires_at
  )
  on conflict (controller_telegram_user_id, chat_id)
  do update set
    persona_id = excluded.persona_id,
    expires_at = excluded.expires_at,
    updated_at = now();

  select organization.name, member.role
  into v_organization_name, v_role
  from public.organization_users as member
  join public.organizations as organization on organization.id = member.organization_id
  where member.telegram_user_id = v_persona.synthetic_telegram_user_id
    and member.active
  order by member.created_at
  limit 1;

  return jsonb_build_object(
    'id', v_persona.id,
    'controller_telegram_user_id', v_persona.controller_telegram_user_id,
    'alias', v_persona.alias,
    'synthetic_telegram_user_id', v_persona.synthetic_telegram_user_id,
    'display_name', v_persona.display_name,
    'telegram_username', v_persona.telegram_username,
    'expires_at', v_expires_at,
    'registered', v_organization_name is not null,
    'organization_name', v_organization_name,
    'role', v_role
  );
end;
$$;

create or replace function public.resolve_telegram_dev_persona(
  p_controller_telegram_user_id bigint,
  p_chat_id bigint,
  p_session_minutes integer
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_persona public.telegram_dev_personas%rowtype;
  v_expires_at timestamptz;
  v_organization_name text;
  v_role public.organization_role;
begin
  if p_session_minutes < 5 or p_session_minutes > 1440 then
    raise exception using
      errcode = '22023',
      message = 'Persona session must be between 5 and 1440 minutes';
  end if;
  if not public.telegram_dev_controller_is_admin(p_controller_telegram_user_id) then
    return null;
  end if;

  select persona.*
  into v_persona
  from public.telegram_dev_persona_sessions as session
  join public.telegram_dev_personas as persona on persona.id = session.persona_id
  where session.controller_telegram_user_id = p_controller_telegram_user_id
    and session.chat_id = p_chat_id
    and session.expires_at > now()
  for update of session, persona;

  if not found then
    delete from public.telegram_dev_persona_sessions
    where controller_telegram_user_id = p_controller_telegram_user_id
      and chat_id = p_chat_id
      and expires_at <= now();
    return null;
  end if;

  v_expires_at := now() + make_interval(mins => p_session_minutes);
  update public.telegram_dev_persona_sessions
  set expires_at = v_expires_at,
      updated_at = now()
  where controller_telegram_user_id = p_controller_telegram_user_id
    and chat_id = p_chat_id;
  update public.telegram_dev_personas
  set last_used_at = now()
  where id = v_persona.id;

  select organization.name, member.role
  into v_organization_name, v_role
  from public.organization_users as member
  join public.organizations as organization on organization.id = member.organization_id
  where member.telegram_user_id = v_persona.synthetic_telegram_user_id
    and member.active
  order by member.created_at
  limit 1;

  return jsonb_build_object(
    'id', v_persona.id,
    'controller_telegram_user_id', v_persona.controller_telegram_user_id,
    'alias', v_persona.alias,
    'synthetic_telegram_user_id', v_persona.synthetic_telegram_user_id,
    'display_name', v_persona.display_name,
    'telegram_username', v_persona.telegram_username,
    'expires_at', v_expires_at,
    'registered', v_organization_name is not null,
    'organization_name', v_organization_name,
    'role', v_role
  );
end;
$$;

create or replace function public.clear_telegram_dev_persona(
  p_controller_telegram_user_id bigint,
  p_chat_id bigint
)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_deleted_count bigint;
begin
  if not public.telegram_dev_controller_is_admin(p_controller_telegram_user_id) then
    raise exception using
      errcode = '42501',
      message = 'Only an active organization admin can simulate Telegram users';
  end if;
  delete from public.telegram_dev_persona_sessions
  where controller_telegram_user_id = p_controller_telegram_user_id
    and chat_id = p_chat_id;
  get diagnostics v_deleted_count = row_count;
  return v_deleted_count > 0;
end;
$$;

create or replace function public.list_telegram_dev_personas(
  p_controller_telegram_user_id bigint,
  p_chat_id bigint
)
returns jsonb
language plpgsql
security definer
stable
set search_path = public, pg_temp
as $$
declare
  v_result jsonb;
begin
  if not public.telegram_dev_controller_is_admin(p_controller_telegram_user_id) then
    raise exception using
      errcode = '42501',
      message = 'Only an active organization admin can simulate Telegram users';
  end if;

  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'id', persona.id,
        'controller_telegram_user_id', persona.controller_telegram_user_id,
        'alias', persona.alias,
        'synthetic_telegram_user_id', persona.synthetic_telegram_user_id,
        'display_name', persona.display_name,
        'telegram_username', persona.telegram_username,
        'active', session.persona_id is not null and session.expires_at > now(),
        'expires_at', session.expires_at,
        'registered', member.id is not null,
        'organization_name', organization.name,
        'role', member.role
      )
      order by persona.alias
    ),
    '[]'::jsonb
  )
  into v_result
  from public.telegram_dev_personas as persona
  left join public.telegram_dev_persona_sessions as session
    on session.controller_telegram_user_id = persona.controller_telegram_user_id
   and session.chat_id = p_chat_id
   and session.persona_id = persona.id
  left join lateral (
    select active_member.*
    from public.organization_users as active_member
    where active_member.telegram_user_id = persona.synthetic_telegram_user_id
      and active_member.active
    order by active_member.created_at
    limit 1
  ) as member on true
  left join public.organizations as organization on organization.id = member.organization_id
  where persona.controller_telegram_user_id = p_controller_telegram_user_id;

  return v_result;
end;
$$;

revoke all on function public.telegram_dev_controller_is_admin(bigint)
  from public, anon, authenticated;
revoke all on function public.activate_telegram_dev_persona(bigint, bigint, text, text, integer)
  from public, anon, authenticated;
revoke all on function public.resolve_telegram_dev_persona(bigint, bigint, integer)
  from public, anon, authenticated;
revoke all on function public.clear_telegram_dev_persona(bigint, bigint)
  from public, anon, authenticated;
revoke all on function public.list_telegram_dev_personas(bigint, bigint)
  from public, anon, authenticated;

grant execute on function public.telegram_dev_controller_is_admin(bigint) to service_role;
grant execute on function public.activate_telegram_dev_persona(bigint, bigint, text, text, integer)
  to service_role;
grant execute on function public.resolve_telegram_dev_persona(bigint, bigint, integer)
  to service_role;
grant execute on function public.clear_telegram_dev_persona(bigint, bigint) to service_role;
grant execute on function public.list_telegram_dev_personas(bigint, bigint) to service_role;

alter function public.enqueue_processing_outcome(
  uuid,
  uuid,
  public.processing_outcome_type,
  uuid,
  bigint,
  jsonb
) rename to enqueue_processing_outcome_internal;

revoke all on function public.enqueue_processing_outcome_internal(
  uuid,
  uuid,
  public.processing_outcome_type,
  uuid,
  bigint,
  jsonb
) from public, anon, authenticated, service_role;

create or replace function public.enqueue_processing_outcome(
  p_organization_id uuid,
  p_source_event_id uuid,
  p_outcome_type public.processing_outcome_type,
  p_aggregate_id uuid,
  p_chat_id bigint,
  p_payload jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_simulation jsonb;
begin
  select source_event.payload -> '_inventory_agent_dev_simulation'
  into v_simulation
  from public.source_events as source_event
  where source_event.organization_id = p_organization_id
    and source_event.id = p_source_event_id;

  return public.enqueue_processing_outcome_internal(
    p_organization_id,
    p_source_event_id,
    p_outcome_type,
    p_aggregate_id,
    p_chat_id,
    coalesce(p_payload, '{}'::jsonb)
      || case
           when v_simulation is null then '{}'::jsonb
           else jsonb_build_object('_dev_simulation', v_simulation)
         end
  );
end;
$$;

revoke all on function public.enqueue_processing_outcome(
  uuid,
  uuid,
  public.processing_outcome_type,
  uuid,
  bigint,
  jsonb
) from public, anon, authenticated;
grant execute on function public.enqueue_processing_outcome(
  uuid,
  uuid,
  public.processing_outcome_type,
  uuid,
  bigint,
  jsonb
) to service_role;

comment on table public.telegram_dev_personas is
  'Development-only stable synthetic Telegram identities controlled by active admins.';
comment on table public.telegram_dev_persona_sessions is
  'Chat-scoped, expiring selection of a development Telegram persona.';
