begin;

create extension if not exists pgtap with schema extensions;

select plan(15);

select has_table(
  'public',
  'telegram_dev_personas',
  'stable development personas have a durable table'
);
select has_table(
  'public',
  'telegram_dev_persona_sessions',
  'chat-scoped development persona sessions have a durable table'
);
select has_function(
  'public',
  'activate_telegram_dev_persona',
  array['bigint', 'bigint', 'text', 'text', 'integer'],
  'persona activation RPC exists'
);
select has_function(
  'public',
  'resolve_telegram_dev_persona',
  array['bigint', 'bigint', 'integer'],
  'persona resolution RPC exists'
);

insert into public.organization_users (
  id,
  organization_id,
  telegram_user_id,
  display_name,
  role
)
values
  (
    '11000000-0000-0000-0000-000000000097',
    '10000000-0000-0000-0000-000000000001',
    299000097,
    'Persona Test Admin',
    'admin'
  ),
  (
    '11000000-0000-0000-0000-000000000098',
    '10000000-0000-0000-0000-000000000001',
    299000098,
    'Persona Test Worker',
    'worker'
  );

select throws_ok(
  $$
    select public.activate_telegram_dev_persona(
      299000098,
      299000098,
      'bob',
      'Bob',
      120
    )
  $$,
  '42501',
  'Only an active organization admin can simulate Telegram users',
  'non-admin controllers cannot simulate users'
);
select throws_ok(
  $$
    select public.activate_telegram_dev_persona(
      299000097,
      299000097,
      'not valid',
      'Not Valid',
      120
    )
  $$,
  '22023',
  'Persona alias must start with a letter and use only letters, numbers, underscores, or hyphens',
  'invalid aliases are rejected'
);

create temporary table activated_persona as
select public.activate_telegram_dev_persona(
  299000097,
  299000097,
  'bob',
  'Bob',
  120
) as result;

select is(
  (select result ->> 'alias' from activated_persona),
  'bob',
  'admin can create and activate Bob'
);
select ok(
  (
    select (result ->> 'synthetic_telegram_user_id')::bigint
      between 4000000000000000 and 4499999999999999
    from activated_persona
  ),
  'synthetic Telegram ID is in the reserved development range'
);
select is(
  (
    select public.resolve_telegram_dev_persona(299000097, 299000097, 120)
      ->> 'synthetic_telegram_user_id'
  ),
  (select result ->> 'synthetic_telegram_user_id' from activated_persona),
  'the selected persona resolves in the same controller chat'
);
select is(
  public.resolve_telegram_dev_persona(299000097, -100123, 120),
  null::jsonb,
  'persona selection does not leak into another chat'
);
select is(
  (
    select public.activate_telegram_dev_persona(
      299000097,
      299000097,
      'bob',
      'Bob',
      120
    ) ->> 'synthetic_telegram_user_id'
  ),
  (select result ->> 'synthetic_telegram_user_id' from activated_persona),
  'reselecting an alias keeps its stable synthetic identity'
);
select is(
  jsonb_array_length(public.list_telegram_dev_personas(299000097, 299000097)),
  1,
  'controller can list the one persona it created'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  payload
)
values (
  '50000000-0000-0000-0000-000000000099',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'dev-persona-outbox-test',
  'message',
  jsonb_build_object(
    'message',
    jsonb_build_object(
      'from',
      jsonb_build_object('id', 4000000000000000),
      'chat',
      jsonb_build_object('id', 299000097),
      'text',
      'hello'
    ),
    '_inventory_agent_dev_simulation',
    jsonb_build_object('alias', 'bob', 'display_name', 'Bob')
  )
);

create temporary table enqueued_persona_outcome as
select public.enqueue_processing_outcome(
  '10000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000099',
  'agent_message',
  null,
  299000097,
  '{"message":"hello"}'::jsonb
);
select is(
  (
    select payload #>> '{_dev_simulation,display_name}'
    from public.processing_outbox
    where source_event_id = '50000000-0000-0000-0000-000000000099'
  ),
  'Bob',
  'simulation audit label follows an event into outbound delivery'
);

update public.telegram_dev_persona_sessions
set expires_at = now() - interval '1 minute'
where controller_telegram_user_id = 299000097
  and chat_id = 299000097;
select is(
  public.resolve_telegram_dev_persona(299000097, 299000097, 120),
  null::jsonb,
  'expired simulation sessions resolve to the real identity'
);

create temporary table reactivated_persona as
select public.activate_telegram_dev_persona(
  299000097,
  299000097,
  'bob',
  'Bob',
  120
);
select ok(
  public.clear_telegram_dev_persona(299000097, 299000097),
  '/user me clears the selected persona'
);

select * from finish();
rollback;
