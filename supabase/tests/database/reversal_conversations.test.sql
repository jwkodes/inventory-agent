begin;

create extension if not exists pgtap with schema extensions;

select plan(30);

select has_table(
  'public',
  'transaction_reversal_requests',
  'durable reversal request table exists'
);
select has_function(
  'public',
  'begin_transaction_reversal_request',
  array['uuid', 'uuid', 'bigint'],
  'reversal request begin function exists'
);
select has_function(
  'public',
  'capture_transaction_reversal_reason',
  array['uuid', 'uuid', 'bigint', 'text'],
  'reversal reason capture function exists'
);
select has_function(
  'public',
  'confirm_transaction_reversal_request',
  array['uuid', 'uuid'],
  'reversal confirmation function exists'
);
select has_function(
  'public',
  'cancel_transaction_reversal_request',
  array['uuid', 'uuid'],
  'reversal cancellation function exists'
);
select ok(
  (
    select class.relrowsecurity
    from pg_class as class
    join pg_namespace as namespace on namespace.oid = class.relnamespace
    where namespace.nspname = 'public'
      and class.relname = 'transaction_reversal_requests'
  ),
  'row level security is enabled on reversal requests'
);

insert into public.inventory_transactions (
  id,
  organization_id,
  location_id,
  transaction_type,
  created_by,
  confirmed_by,
  notes
)
values (
  '60000000-0000-0000-0000-000000000100',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  'receive',
  '11000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  'Reversal conversation test receipt'
);

insert into public.transaction_lines (
  id,
  organization_id,
  transaction_id,
  line_number,
  item_variant_id,
  quantity_delta,
  base_unit,
  quantity_before,
  quantity_after
)
values (
  '61000000-0000-0000-0000-000000000100',
  '10000000-0000-0000-0000-000000000001',
  '60000000-0000-0000-0000-000000000100',
  1,
  '21000000-0000-0000-0000-000000000002',
  3,
  'each',
  200,
  203
);

insert into public.stock_movements (
  organization_id,
  transaction_id,
  transaction_line_id,
  location_id,
  item_variant_id,
  quantity_delta
)
values (
  '10000000-0000-0000-0000-000000000001',
  '60000000-0000-0000-0000-000000000100',
  '61000000-0000-0000-0000-000000000100',
  '12000000-0000-0000-0000-000000000001',
  '21000000-0000-0000-0000-000000000002',
  3
);

update public.inventory_balances
set quantity = 203
where id = '30000000-0000-0000-0000-000000000002';

create temporary table reversal_request as
select public.begin_transaction_reversal_request(
  '60000000-0000-0000-0000-000000000100',
  '11000000-0000-0000-0000-000000000001',
  100000001
) as request_id;

select is(
  (
    select request.status::text
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from reversal_request)
  ),
  'awaiting_reason',
  'begin creates an awaiting-reason request'
);
select is(
  (
    select request.transaction_id
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from reversal_request)
  ),
  '60000000-0000-0000-0000-000000000100'::uuid,
  'request is bound to the original transaction'
);
select is(
  public.begin_transaction_reversal_request(
    '60000000-0000-0000-0000-000000000100',
    '11000000-0000-0000-0000-000000000001',
    100000001
  ),
  (select request_id from reversal_request),
  'repeated begin is idempotent'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  status,
  payload,
  processing_started_at,
  processing_attempts
)
values (
  '50000000-0000-0000-0000-000000000100',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'reversal-reason-event',
  'message',
  'processing',
  '{
    "message": {
      "from": {"id": 100000001},
      "chat": {"id": 100000001},
      "text": "  Duplicate supplier delivery  "
    }
  }'::jsonb,
  now(),
  1
);

select is(
  public.capture_transaction_reversal_reason(
    '50000000-0000-0000-0000-000000000100',
    '11000000-0000-0000-0000-000000000001',
    100000001,
    '  Duplicate supplier delivery  '
  ),
  (select request_id from reversal_request),
  'the next claimed message is captured as the reason'
);
select is(
  (
    select request.status::text
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from reversal_request)
  ),
  'awaiting_confirmation',
  'reason capture advances to final confirmation'
);
select is(
  public.begin_transaction_reversal_request(
    '60000000-0000-0000-0000-000000000100',
    '11000000-0000-0000-0000-000000000001',
    100000001
  ),
  (select request_id from reversal_request),
  'a retried begin does not erase an already captured reason'
);
select is(
  (
    select request.reason
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from reversal_request)
  ),
  'Duplicate supplier delivery',
  'the audited reason is trimmed and retained'
);
select is(
  (
    select request.reason_source_event_id
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from reversal_request)
  ),
  '50000000-0000-0000-0000-000000000100'::uuid,
  'the reason retains its source event'
);
select is(
  public.capture_transaction_reversal_reason(
    '50000000-0000-0000-0000-000000000100',
    '11000000-0000-0000-0000-000000000001',
    100000001,
    'Duplicate supplier delivery'
  ),
  (select request_id from reversal_request),
  'reason capture is idempotent for an event retry'
);
select lives_ok(
  $$
    select public.enqueue_processing_outcome(
      '10000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000100',
      'reversal_confirmation',
      (select request_id from reversal_request),
      100000001,
      '{"reason":"Duplicate supplier delivery"}'::jsonb
    )
  $$,
  'captured reason can enqueue a tenant-bound reversal confirmation'
);
select is(
  (
    select outbox.outcome_type::text
    from public.processing_outbox as outbox
    where outbox.source_event_id = '50000000-0000-0000-0000-000000000100'
  ),
  'reversal_confirmation',
  'reversal confirmation outcome is retained in the durable outbox'
);
select throws_like(
  $$
    select public.enqueue_processing_outcome(
      '10000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000100',
      'reversal_confirmation',
      (select request_id from reversal_request),
      100000001,
      '{"reason":"A different reason"}'::jsonb
    )
  $$,
  '%Pending reversal confirmation does not match%',
  'outbox cannot present a reason different from the durable request'
);

create temporary table completed_reversal as
select public.confirm_transaction_reversal_request(
  (select request_id from reversal_request),
  '11000000-0000-0000-0000-000000000001'
) as reversal_id;

select is(
  (
    select request.reversal_transaction_id
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from reversal_request)
  ),
  (select reversal_id from completed_reversal),
  'confirmation retains the compensating transaction ID'
);
select is(
  (
    select request.status::text
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from reversal_request)
  ),
  'completed',
  'confirmation completes the durable request'
);
select is(
  (
    select transaction.reason
    from public.inventory_transactions as transaction
    where transaction.id = (select reversal_id from completed_reversal)
  ),
  'Duplicate supplier delivery',
  'the reason is copied onto the immutable reversal transaction'
);
select is(
  (
    select balance.quantity
    from public.inventory_balances as balance
    where balance.id = '30000000-0000-0000-0000-000000000002'
  ),
  200::numeric,
  'the compensating transaction restores stock'
);
select is(
  (
    select count(*)
    from public.inventory_transactions as transaction
    where transaction.reversal_of_transaction_id =
      '60000000-0000-0000-0000-000000000100'
  ),
  1::bigint,
  'exactly one reversal transaction is created'
);
select is(
  public.confirm_transaction_reversal_request(
    (select request_id from reversal_request),
    '11000000-0000-0000-0000-000000000001'
  ),
  (select reversal_id from completed_reversal),
  'repeated final confirmation is idempotent'
);

insert into public.organization_users (
  id,
  organization_id,
  telegram_user_id,
  display_name,
  role
)
values (
  '11000000-0000-0000-0000-000000000100',
  '10000000-0000-0000-0000-000000000001',
  100000100,
  'Test Worker',
  'worker'
);

insert into public.inventory_transactions (
  id,
  organization_id,
  location_id,
  transaction_type,
  created_by,
  confirmed_by,
  notes
)
values (
  '60000000-0000-0000-0000-000000000101',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  'adjustment',
  '11000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  'Cancellation test transaction'
);

select throws_like(
  $$
    select public.begin_transaction_reversal_request(
      '60000000-0000-0000-0000-000000000101',
      '11000000-0000-0000-0000-000000000100',
      100000100
    )
  $$,
  '%Only an active manager or admin%',
  'ordinary workers cannot request reversal'
);

create temporary table cancelled_request as
select public.begin_transaction_reversal_request(
  '60000000-0000-0000-0000-000000000101',
  '11000000-0000-0000-0000-000000000001',
  100000001
) as request_id;

select is(
  (
    select request.status::text
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from cancelled_request)
  ),
  'awaiting_reason',
  'a second authorized request starts reason collection'
);
select is(
  public.cancel_transaction_reversal_request(
    (select request_id from cancelled_request),
    '11000000-0000-0000-0000-000000000001'
  ),
  (select request_id from cancelled_request),
  'pending reversal can be cancelled'
);
select is(
  (
    select request.status::text
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from cancelled_request)
  ),
  'cancelled',
  'cancellation is retained'
);
select isnt(
  (
    select request.completed_at
    from public.transaction_reversal_requests as request
    where request.id = (select request_id from cancelled_request)
  ),
  null::timestamptz,
  'cancelled request records its completion time'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  status,
  payload,
  processing_started_at,
  processing_attempts
)
values (
  '50000000-0000-0000-0000-000000000101',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'ordinary-message-after-cancel',
  'message',
  'processing',
  '{
    "message": {
      "from": {"id": 100000001},
      "chat": {"id": 100000001},
      "text": "receive more milk"
    }
  }'::jsonb,
  now(),
  1
);

select is(
  public.capture_transaction_reversal_reason(
    '50000000-0000-0000-0000-000000000101',
    '11000000-0000-0000-0000-000000000001',
    100000001,
    'receive more milk'
  ),
  null::uuid,
  'ordinary text is not consumed when no reversal is awaiting a reason'
);

select * from finish();
rollback;
