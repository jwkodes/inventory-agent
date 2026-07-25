create type public.organization_registration_request_status as enum (
  'pending',
  'rejection_notifying'
);

create type public.registration_notification_kind as enum (
  'registration_pending',
  'registration_approved',
  'registration_rejected',
  'registration_invalid',
  'registration_already_registered'
);

create table public.organization_registration_invites (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  code_hash text not null unique,
  code_hint text not null,
  expires_at timestamptz not null,
  max_uses integer not null default 1,
  use_count integer not null default 0,
  active boolean not null default true,
  created_by uuid not null,
  created_at timestamptz not null default now(),
  revoked_at timestamptz,
  foreign key (organization_id, created_by)
    references public.organization_users (organization_id, id),
  check (code_hash ~ '^[0-9a-f]{64}$'),
  check (length(code_hint) between 2 and 12),
  check (max_uses between 1 and 1000),
  check (use_count between 0 and max_uses),
  check ((active and revoked_at is null) or (not active))
);

create table public.organization_registration_requests (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  invite_id uuid not null references public.organization_registration_invites (id),
  telegram_user_id bigint not null,
  telegram_username text,
  display_name text not null,
  source_chat_id bigint not null,
  status public.organization_registration_request_status not null default 'pending',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, telegram_user_id),
  check (telegram_user_id > 0),
  check (source_chat_id > 0),
  check (length(trim(display_name)) between 1 and 200),
  check (telegram_username is null or length(telegram_username) between 1 and 64)
);

create table public.organization_membership_changes (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  organization_user_id uuid not null,
  actor_id uuid not null,
  action text not null,
  role public.organization_role not null,
  created_at timestamptz not null default now(),
  foreign key (organization_id, organization_user_id)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, actor_id)
    references public.organization_users (organization_id, id),
  check (action in ('registration_approved', 'role_changed'))
);

create table public.registration_telegram_notifications (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid references public.organizations (id) on delete cascade,
  registration_request_id uuid
    references public.organization_registration_requests (id) on delete cascade,
  chat_id bigint not null,
  kind public.registration_notification_kind not null,
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'pending',
  attempts integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  processing_started_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  check (chat_id > 0),
  check (jsonb_typeof(payload) = 'object'),
  check (status in ('pending', 'processing')),
  check (attempts >= 0)
);

create index organization_registration_invites_org_created_idx
  on public.organization_registration_invites (organization_id, created_at desc);
create index organization_registration_requests_org_created_idx
  on public.organization_registration_requests (organization_id, created_at desc);
create index registration_telegram_notifications_due_idx
  on public.registration_telegram_notifications (next_attempt_at, created_at)
  where status = 'pending';

alter table public.organization_registration_invites enable row level security;
alter table public.organization_registration_requests enable row level security;
alter table public.organization_membership_changes enable row level security;
alter table public.registration_telegram_notifications enable row level security;

grant select, insert, update on public.organization_registration_invites to service_role;
grant select, insert, update, delete on public.organization_registration_requests to service_role;
grant select, insert on public.organization_membership_changes to service_role;
grant select, insert, update, delete on public.registration_telegram_notifications to service_role;

create or replace function public.create_organization_registration_invite(
  p_organization_id uuid,
  p_actor_id uuid,
  p_code_hash text,
  p_code_hint text,
  p_expires_at timestamptz,
  p_max_uses integer default 1
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_invite public.organization_registration_invites%rowtype;
begin
  if not exists (
    select 1
    from public.organization_users as member
    where member.organization_id = p_organization_id
      and member.id = p_actor_id
      and member.active
      and member.role = 'admin'
  ) then
    raise exception using errcode = '42501', message = 'Only an active organization admin can create invites';
  end if;
  if p_code_hash !~ '^[0-9a-f]{64}$' then
    raise exception using errcode = '22023', message = 'Invite code hash is invalid';
  end if;
  if p_expires_at <= now() then
    raise exception using errcode = '22023', message = 'Invite expiry must be in the future';
  end if;
  if p_max_uses < 1 or p_max_uses > 1000 then
    raise exception using errcode = '22023', message = 'Invite max uses must be between 1 and 1000';
  end if;

  insert into public.organization_registration_invites (
    organization_id,
    code_hash,
    code_hint,
    expires_at,
    max_uses,
    created_by
  )
  values (
    p_organization_id,
    lower(p_code_hash),
    p_code_hint,
    p_expires_at,
    p_max_uses,
    p_actor_id
  )
  returning * into v_invite;

  return to_jsonb(v_invite) - 'code_hash';
end;
$$;

create or replace function public.submit_organization_registration(
  p_code_hash text,
  p_telegram_user_id bigint,
  p_telegram_username text,
  p_display_name text,
  p_source_chat_id bigint
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_invite public.organization_registration_invites%rowtype;
  v_request public.organization_registration_requests%rowtype;
  v_organization_name text;
begin
  if p_telegram_user_id <= 0
     or p_source_chat_id <= 0
     or nullif(trim(p_display_name), '') is null then
    raise exception using errcode = '22023', message = 'Valid private Telegram identity is required';
  end if;

  if exists (
    select 1
    from public.organization_users as member
    where member.telegram_user_id = p_telegram_user_id
      and member.active
  ) then
    insert into public.registration_telegram_notifications (chat_id, kind)
    values (p_source_chat_id, 'registration_already_registered');
    return jsonb_build_object('status', 'already_registered');
  end if;

  select invite.* into v_invite
  from public.organization_registration_invites as invite
  where invite.code_hash = lower(p_code_hash)
    and invite.active
    and invite.revoked_at is null
    and invite.expires_at > now()
    and invite.use_count < invite.max_uses
  for update;

  if not found then
    insert into public.registration_telegram_notifications (chat_id, kind)
    values (p_source_chat_id, 'registration_invalid');
    return jsonb_build_object('status', 'invalid_invite');
  end if;

  select request.* into v_request
  from public.organization_registration_requests as request
  where request.organization_id = v_invite.organization_id
    and request.telegram_user_id = p_telegram_user_id;

  if found then
    select organization.name into v_organization_name
    from public.organizations as organization
    where organization.id = v_request.organization_id;
    insert into public.registration_telegram_notifications (
      organization_id,
      registration_request_id,
      chat_id,
      kind,
      payload
    )
    values (
      v_request.organization_id,
      v_request.id,
      v_request.source_chat_id,
      'registration_pending',
      jsonb_build_object('organization_name', v_organization_name)
    );
    return jsonb_build_object(
      'status', 'already_pending',
      'request_id', v_request.id,
      'organization_id', v_request.organization_id
    );
  end if;

  select organization.name into v_organization_name
  from public.organizations as organization
  where organization.id = v_invite.organization_id;

  insert into public.organization_registration_requests (
    organization_id,
    invite_id,
    telegram_user_id,
    telegram_username,
    display_name,
    source_chat_id
  )
  values (
    v_invite.organization_id,
    v_invite.id,
    p_telegram_user_id,
    nullif(trim(p_telegram_username), ''),
    trim(p_display_name),
    p_source_chat_id
  )
  returning * into v_request;

  update public.organization_registration_invites
  set use_count = use_count + 1,
      active = case when use_count + 1 >= max_uses then false else active end
  where id = v_invite.id;

  insert into public.registration_telegram_notifications (
    organization_id,
    registration_request_id,
    chat_id,
    kind,
    payload
  )
  values (
    v_request.organization_id,
    v_request.id,
    v_request.source_chat_id,
    'registration_pending',
    jsonb_build_object('organization_name', v_organization_name)
  );

  return jsonb_build_object(
    'status', 'pending',
    'request_id', v_request.id,
    'organization_id', v_request.organization_id
  );
end;
$$;

create or replace function public.approve_organization_registration(
  p_registration_request_id uuid,
  p_actor_id uuid,
  p_role public.organization_role
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_request public.organization_registration_requests%rowtype;
  v_member public.organization_users%rowtype;
  v_organization_name text;
begin
  select request.* into v_request
  from public.organization_registration_requests as request
  where request.id = p_registration_request_id
    and request.status = 'pending'
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Pending registration request was not found';
  end if;
  if not exists (
    select 1
    from public.organization_users as actor
    where actor.organization_id = v_request.organization_id
      and actor.id = p_actor_id
      and actor.active
      and actor.role = 'admin'
  ) then
    raise exception using errcode = '42501', message = 'Only an active organization admin can approve registrations';
  end if;

  insert into public.organization_users (
    organization_id,
    telegram_user_id,
    display_name,
    role
  )
  values (
    v_request.organization_id,
    v_request.telegram_user_id,
    v_request.display_name,
    p_role
  )
  on conflict (organization_id, telegram_user_id)
  do update set
    display_name = excluded.display_name,
    role = excluded.role,
    active = true
  returning * into v_member;

  insert into public.organization_membership_changes (
    organization_id,
    organization_user_id,
    actor_id,
    action,
    role
  )
  values (
    v_request.organization_id,
    v_member.id,
    p_actor_id,
    'registration_approved',
    p_role
  );

  select organization.name into v_organization_name
  from public.organizations as organization
  where organization.id = v_request.organization_id;

  delete from public.registration_telegram_notifications
  where registration_request_id = v_request.id
    and kind = 'registration_pending';

  insert into public.registration_telegram_notifications (
    organization_id,
    chat_id,
    kind,
    payload
  )
  values (
    v_request.organization_id,
    v_request.source_chat_id,
    'registration_approved',
    jsonb_build_object(
      'organization_name', v_organization_name,
      'role', p_role
    )
  );

  delete from public.organization_registration_requests
  where id = v_request.id;

  return jsonb_build_object(
    'status', 'approved',
    'member_id', v_member.id,
    'organization_id', v_member.organization_id,
    'role', v_member.role
  );
end;
$$;

create or replace function public.reject_organization_registration(
  p_registration_request_id uuid,
  p_actor_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  v_request public.organization_registration_requests%rowtype;
begin
  select request.* into v_request
  from public.organization_registration_requests as request
  where request.id = p_registration_request_id
    and request.status = 'pending'
  for update;
  if not found then
    raise exception using errcode = 'P0002', message = 'Pending registration request was not found';
  end if;
  if not exists (
    select 1
    from public.organization_users as actor
    where actor.organization_id = v_request.organization_id
      and actor.id = p_actor_id
      and actor.active
      and actor.role = 'admin'
  ) then
    raise exception using errcode = '42501', message = 'Only an active organization admin can reject registrations';
  end if;

  update public.organization_registration_requests
  set status = 'rejection_notifying',
      updated_at = now()
  where id = v_request.id;

  delete from public.registration_telegram_notifications
  where registration_request_id = v_request.id
    and kind = 'registration_pending';

  insert into public.registration_telegram_notifications (
    organization_id,
    registration_request_id,
    chat_id,
    kind
  )
  values (
    v_request.organization_id,
    v_request.id,
    v_request.source_chat_id,
    'registration_rejected'
  );

  return jsonb_build_object('status', 'rejection_notifying', 'request_id', v_request.id);
end;
$$;

create or replace function public.claim_registration_telegram_notification()
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_notification public.registration_telegram_notifications%rowtype;
begin
  update public.registration_telegram_notifications
  set status = 'pending',
      processing_started_at = null,
      next_attempt_at = now()
  where status = 'processing'
    and processing_started_at < now() - interval '15 minutes';

  select notification.* into v_notification
  from public.registration_telegram_notifications as notification
  where notification.status = 'pending'
    and notification.next_attempt_at <= now()
  order by notification.created_at, notification.id
  for update skip locked
  limit 1;

  if not found then
    return null;
  end if;

  update public.registration_telegram_notifications
  set status = 'processing',
      attempts = attempts + 1,
      processing_started_at = now()
  where id = v_notification.id;

  return jsonb_build_object(
    'id', v_notification.id,
    'chat_id', v_notification.chat_id,
    'kind', v_notification.kind,
    'payload', v_notification.payload,
    'attempts', v_notification.attempts + 1
  );
end;
$$;

create or replace function public.complete_registration_telegram_notification(
  p_notification_id uuid,
  p_delivered boolean,
  p_error text default null
)
returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_notification public.registration_telegram_notifications%rowtype;
begin
  select notification.* into v_notification
  from public.registration_telegram_notifications as notification
  where notification.id = p_notification_id
    and notification.status = 'processing'
  for update;
  if not found then
    return 'not_found';
  end if;

  if not p_delivered then
    update public.registration_telegram_notifications
    set status = 'pending',
        processing_started_at = null,
        next_attempt_at = now() + least(
          interval '15 minutes',
          make_interval(secs => greatest(5, power(2, least(attempts, 9))::integer))
        ),
        last_error = left(coalesce(nullif(trim(p_error), ''), 'Telegram delivery failed'), 1000)
    where id = v_notification.id;
    return 'retry_scheduled';
  end if;

  if v_notification.kind = 'registration_rejected'
     and v_notification.registration_request_id is not null then
    delete from public.organization_registration_requests
    where id = v_notification.registration_request_id
      and status = 'rejection_notifying';
  end if;

  delete from public.registration_telegram_notifications
  where id = v_notification.id;
  return 'delivered';
end;
$$;

revoke all on function public.create_organization_registration_invite(
  uuid, uuid, text, text, timestamptz, integer
) from public, anon, authenticated;
revoke all on function public.submit_organization_registration(
  text, bigint, text, text, bigint
) from public, anon, authenticated;
revoke all on function public.approve_organization_registration(
  uuid, uuid, public.organization_role
) from public, anon, authenticated;
revoke all on function public.reject_organization_registration(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.claim_registration_telegram_notification()
  from public, anon, authenticated;
revoke all on function public.complete_registration_telegram_notification(uuid, boolean, text)
  from public, anon, authenticated;

grant execute on function public.create_organization_registration_invite(
  uuid, uuid, text, text, timestamptz, integer
) to service_role;
grant execute on function public.submit_organization_registration(
  text, bigint, text, text, bigint
) to service_role;
grant execute on function public.approve_organization_registration(
  uuid, uuid, public.organization_role
) to service_role;
grant execute on function public.reject_organization_registration(uuid, uuid)
  to service_role;
grant execute on function public.claim_registration_telegram_notification()
  to service_role;
grant execute on function public.complete_registration_telegram_notification(uuid, boolean, text)
  to service_role;

update public.organization_users
set role = 'admin'
where id = '11000000-0000-0000-0000-000000000001'
  and organization_id = '10000000-0000-0000-0000-000000000001';
