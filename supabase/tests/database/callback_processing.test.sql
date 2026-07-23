begin;

create extension if not exists pgtap with schema extensions;

select plan(12);

select has_function(
  'public',
  'claim_telegram_callback_event',
  array['uuid'],
  'callback claim-by-ID function exists'
);
select has_function(
  'public',
  'claim_next_telegram_callback_event',
  array[]::text[],
  'callback claim-next function exists'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type, payload
)
values (
  '50000000-0000-0000-0000-000000000010',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'callback-processing-test',
  'callback_query',
  '{
    "callback_query": {
      "id": "callback-query-10",
      "from": {"id": 100000001},
      "data": "c.opaque-data",
      "message": {
        "message_id": 57,
        "chat": {"id": -100123456789}
      }
    }
  }'::jsonb
);

create temporary table claimed_callback as
select * from public.claim_next_telegram_callback_event();

select is((select count(*) from claimed_callback), 1::bigint, 'callback is claimed once');
select is(
  (select organization_user_id from claimed_callback),
  '11000000-0000-0000-0000-000000000001'::uuid,
  'callback claim resolves the active organization member'
);
select is(
  (select callback_query_id from claimed_callback),
  'callback-query-10',
  'callback query ID is returned'
);
select is(
  (select callback_data from claimed_callback),
  'c.opaque-data',
  'opaque callback data is returned unchanged'
);
select is(
  (select chat_id from claimed_callback),
  (-100123456789)::bigint,
  'callback source chat is returned'
);
select is(
  (select telegram_message_id from claimed_callback),
  57::bigint,
  'callback source message is returned'
);
select is(
  (
    select status::text from public.source_events
    where id = '50000000-0000-0000-0000-000000000010'
  ),
  'processing',
  'callback claim records the processing state'
);
select is(
  (select count(*) from public.claim_next_telegram_callback_event()),
  0::bigint,
  'active callback claim cannot be duplicated'
);
select ok(
  public.finish_source_event(
    '50000000-0000-0000-0000-000000000010', true, null
  ),
  'callback event can use the shared completion function'
);
select is(
  (
    select status::text from public.source_events
    where id = '50000000-0000-0000-0000-000000000010'
  ),
  'processed',
  'callback completion records the processed state'
);

select * from finish();
rollback;
