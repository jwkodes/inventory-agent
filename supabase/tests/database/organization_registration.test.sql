begin;

create extension if not exists pgtap with schema extensions;

select plan(18);

select has_table(
  'public',
  'organization_registration_invites',
  'registration invites are durable'
);
select has_table(
  'public',
  'organization_registration_requests',
  'pending registration requests are durable'
);
select has_table(
  'public',
  'organization_membership_changes',
  'membership decisions are audited'
);
select has_table(
  'public',
  'registration_telegram_notifications',
  'registration notifications use a durable queue'
);
select has_function(
  'public',
  'submit_organization_registration',
  array['text', 'bigint', 'text', 'text', 'bigint'],
  'private registration submission function exists'
);
select has_function(
  'public',
  'approve_organization_registration',
  array['uuid', 'uuid', 'organization_role'],
  'admin approval function exists'
);
select has_function(
  'public',
  'reject_organization_registration',
  array['uuid', 'uuid'],
  'admin rejection function exists'
);

select is(
  (
    public.create_organization_registration_invite(
      '10000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001',
      repeat('a', 64),
      'ABC123',
      now() + interval '1 day',
      2
    ) ->> 'code_hint'
  ),
  'ABC123',
  'an active admin can create a hashed invite'
);

select is(
  (
    public.submit_organization_registration(
      repeat('a', 64),
      200000001,
      'candidate_one',
      'Candidate One',
      200000001
    ) ->> 'status'
  ),
  'pending',
  'a valid invite creates a pending request'
);
select is(
  (
    select count(*)
    from public.organization_registration_requests
    where telegram_user_id = 200000001
      and telegram_username = 'candidate_one'
  ),
  1::bigint,
  'pending applicant identity is stored for admin review'
);

create temporary table first_notice as
select public.claim_registration_telegram_notification() as notification;
select is(
  (select notification ->> 'kind' from first_notice),
  'registration_pending',
  'submission queues a pending notification'
);
select is(
  public.complete_registration_telegram_notification(
    ((select notification ->> 'id' from first_notice))::uuid,
    true,
    null
  ),
  'delivered',
  'pending notification can be completed'
);

select is(
  (
    public.reject_organization_registration(
      (
        select id from public.organization_registration_requests
        where telegram_user_id = 200000001
      ),
      '11000000-0000-0000-0000-000000000001'
    ) ->> 'status'
  ),
  'rejection_notifying',
  'rejection waits for its Telegram notification'
);
select is(
  (
    select count(*) from public.organization_registration_requests
    where telegram_user_id = 200000001
  ),
  1::bigint,
  'rejected applicant data remains until notification succeeds'
);

create temporary table rejection_notice as
select public.claim_registration_telegram_notification() as notification;
select is(
  public.complete_registration_telegram_notification(
    ((select notification ->> 'id' from rejection_notice))::uuid,
    true,
    null
  ),
  'delivered',
  'rejection notification completes successfully'
);
select is(
  (
    select count(*) from public.organization_registration_requests
    where telegram_user_id = 200000001
  ),
  0::bigint,
  'rejected applicant PII is deleted after notification'
);

select is(
  (
    public.submit_organization_registration(
      repeat('a', 64),
      200000002,
      'candidate_two',
      'Candidate Two',
      200000002
    ) ->> 'status'
  ),
  'pending',
  'a multi-use invite accepts its second applicant'
);
select is(
  (
    public.approve_organization_registration(
      (
        select id from public.organization_registration_requests
        where telegram_user_id = 200000002
      ),
      '11000000-0000-0000-0000-000000000001',
      'manager'
    ) ->> 'role'
  ),
  'manager',
  'admin approval creates the selected role'
);

select * from finish();
rollback;
