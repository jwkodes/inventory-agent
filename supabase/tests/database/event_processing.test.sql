begin;

create extension if not exists pgtap with schema extensions;

select plan(28);

select has_table('public', 'processing_outbox', 'processing outbox exists');

select has_function(
  'public',
  'claim_telegram_text_event',
  array['uuid'],
  'atomic Telegram text claim function exists'
);

select has_function(
  'public',
  'finish_source_event',
  array['uuid', 'boolean', 'text'],
  'source event completion function exists'
);

select has_function(
  'public',
  'enqueue_processing_outcome',
  array['uuid', 'uuid', 'processing_outcome_type', 'uuid', 'bigint', 'jsonb'],
  'durable outcome function exists'
);
select has_function(
  'public',
  'claim_next_telegram_text_event',
  array[]::text[],
  'claim-next function exists for continuous workers'
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
  '50000000-0000-0000-0000-000000000004',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'event-processing-test-update',
  'message',
  '{
    "update_id": 70004,
    "message": {
      "message_id": 40,
      "from": {"id": 100000001},
      "chat": {"id": -100123456789},
      "text": "received three AMOX-500"
    }
  }'::jsonb
);

create temporary table claimed_event as
select * from public.claim_telegram_text_event(
  '50000000-0000-0000-0000-000000000004'
);

select is((select count(*) from claimed_event), 1::bigint, 'received event is claimed once');
select is(
  (select organization_user_id from claimed_event),
  '11000000-0000-0000-0000-000000000001'::uuid,
  'claim resolves the active organization member'
);
select is(
  (select location_id from claimed_event),
  '12000000-0000-0000-0000-000000000001'::uuid,
  'claim resolves an active organization location'
);
select is(
  (select message_text from claimed_event),
  'received three AMOX-500',
  'claim returns the source text exactly'
);
select is(
  (
    select status::text from public.source_events
    where id = '50000000-0000-0000-0000-000000000004'
  ),
  'processing',
  'claim moves the event to processing'
);
select is(
  (
    select processing_attempts from public.source_events
    where id = '50000000-0000-0000-0000-000000000004'
  ),
  1,
  'claim records the processing attempt'
);
select is(
  (
    select count(*) from public.claim_telegram_text_event(
      '50000000-0000-0000-0000-000000000004'
    )
  ),
  0::bigint,
  'a processing event cannot be claimed twice'
);

update public.source_events
set processing_started_at = now() - interval '16 minutes'
where id = '50000000-0000-0000-0000-000000000004';

create temporary table reclaimed_event as
select * from public.claim_telegram_text_event(
  '50000000-0000-0000-0000-000000000004'
);

select is(
  (select count(*) from reclaimed_event),
  1::bigint,
  'an abandoned processing claim can be recovered after its lease'
);
select is(
  (
    select processing_attempts from public.source_events
    where id = '50000000-0000-0000-0000-000000000004'
  ),
  2,
  'a recovered claim increments the processing attempt count'
);
select ok(
  public.finish_source_event(
    '50000000-0000-0000-0000-000000000004',
    true,
    null
  ),
  'the claimed event can be completed'
);
select is(
  (
    select status::text from public.source_events
    where id = '50000000-0000-0000-0000-000000000004'
  ),
  'processed',
  'completion records the processed state'
);
select lives_ok(
  $$
    select public.enqueue_processing_outcome(
      '10000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000004',
      'clarification_required',
      null,
      -100123456789,
      '{"message":"Which item did you receive?"}'::jsonb
    )
  $$,
  'a processing outcome can be enqueued'
);
select is(
  (
    select count(*) from public.processing_outbox
    where source_event_id = '50000000-0000-0000-0000-000000000004'
  ),
  1::bigint,
  'one durable outbox row is stored'
);
select lives_ok(
  $$
    select public.enqueue_processing_outcome(
      '10000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000004',
      'clarification_required',
      null,
      -100123456789,
      '{"message":"retry"}'::jsonb
    )
  $$,
  'enqueue retry is idempotent'
);
select is(
  (
    select count(*) from public.processing_outbox
    where source_event_id = '50000000-0000-0000-0000-000000000004'
  ),
  1::bigint,
  'enqueue retry does not duplicate delivery work'
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
  '50000000-0000-0000-0000-000000000008',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'event-processing-claim-next',
  'message',
  '{
    "message": {
      "from": {"id": 100000001},
      "chat": {"id": 100000001},
      "text": "received one MILK-FULLCREAM-1L"
    }
  }'::jsonb
);

create temporary table next_claimed_event as
select * from public.claim_next_telegram_text_event();

select is(
  (select event_id from next_claimed_event),
  '50000000-0000-0000-0000-000000000008'::uuid,
  'claim-next returns the oldest eligible Telegram text event'
);
select is(
  (select message_text from next_claimed_event),
  'received one MILK-FULLCREAM-1L',
  'claim-next returns the message context'
);
select ok(
  public.finish_source_event(
    '50000000-0000-0000-0000-000000000008',
    false,
    'temporary model failure'
  ),
  'first processing failure is recorded'
);
select is(
  (
    select status::text from public.source_events
    where id = '50000000-0000-0000-0000-000000000008'
  ),
  'received',
  'first failure returns the event to the queue'
);
select is(
  (select count(*) from public.claim_next_telegram_text_event()),
  0::bigint,
  'retry delay prevents an immediate reclaim'
);

update public.source_events
set next_attempt_at = now() - interval '1 second',
    processing_attempts = 2
where id = '50000000-0000-0000-0000-000000000008';

create temporary table final_claimed_event as
select * from public.claim_next_telegram_text_event();

select is(
  (
    select processing_attempts from public.source_events
    where id = '50000000-0000-0000-0000-000000000008'
  ),
  3,
  'the next claim records the third processing attempt'
);
select ok(
  public.finish_source_event(
    '50000000-0000-0000-0000-000000000008',
    false,
    'third model failure'
  ),
  'third processing failure is recorded'
);
select is(
  (
    select status::text from public.source_events
    where id = '50000000-0000-0000-0000-000000000008'
  ),
  'failed',
  'third failure dead-letters the source event'
);

select * from finish();
rollback;
