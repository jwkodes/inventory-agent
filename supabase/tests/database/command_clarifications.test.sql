begin;

create extension if not exists pgtap with schema extensions;

select plan(20);

select has_table(
  'public',
  'command_clarification_requests',
  'command clarifications are durable'
);
select has_function(
  'public',
  'begin_command_clarification',
  array['uuid', 'uuid', 'bigint', 'text', 'jsonb'],
  'command clarification creation function exists'
);
select has_function(
  'public',
  'find_pending_command_clarification',
  array['uuid', 'bigint'],
  'pending command clarification lookup exists'
);
select has_function(
  'public',
  'get_command_clarification_view',
  array['uuid'],
  'command clarification view exists'
);
select has_function(
  'public',
  'continue_command_clarification',
  array['uuid', 'uuid', 'uuid', 'text', 'text', 'jsonb'],
  'command clarification continuation exists'
);
select has_function(
  'public',
  'resolve_command_clarification',
  array['uuid', 'uuid', 'uuid', 'text', 'jsonb', 'uuid'],
  'command clarification resolution exists'
);
select has_function(
  'public',
  'cancel_command_clarification',
  array['uuid', 'uuid', 'uuid'],
  'command clarification cancellation exists'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  status,
  payload,
  processed_at
)
values (
  '50000000-0000-0000-0000-000000000091',
  '10000000-0000-0000-0000-000000000001',
  'database_test',
  'command-clarification-image',
  'invoice_image',
  'processed',
  '{}'::jsonb,
  now()
);

create temporary table command_clarification_fixture as
select public.begin_command_clarification(
  '50000000-0000-0000-0000-000000000091',
  '11000000-0000-0000-0000-000000000001',
  123,
  'Should these invoice lines be recorded as received stock?',
  $json$
  {
    "command": {
      "schema_version": "1.0",
      "intent": "UNKNOWN",
      "location_hint": null,
      "lines": [{
        "source_text": "ABC-123 3 boxes",
        "item_reference": {"type": "PART_NUMBER", "value": "ABC-123"},
        "description": "Invoice Widget",
        "quantity": "3",
        "unit": "box",
        "attributes": []
      }],
      "notes": "invoice",
      "needs_clarification": true,
      "clarification_question": "Should these invoice lines be recorded as received stock?"
    },
    "response_id": "response-image",
    "model": "gpt-test",
    "prompt_version": "inventory-invoice-image-v1",
    "input_tokens": 10,
    "output_tokens": 5,
    "total_tokens": 15
  }
  $json$::jsonb
) as request_id;

select ok(
  (select request_id is not null from command_clarification_fixture),
  'ambiguous image extraction creates a pending clarification'
);
select is(
  public.find_pending_command_clarification(
    '11000000-0000-0000-0000-000000000001',
    123
  ),
  (select request_id from command_clarification_fixture),
  'pending clarification is scoped to the actor and chat'
);
select is(
  (
    public.get_command_clarification_view(
      (select request_id from command_clarification_fixture)
    ) #>> '{extraction,command,lines,0,quantity}'
  ),
  '3',
  'the exact extracted invoice quantity survives the clarification boundary'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  status,
  payload,
  processed_at
)
values (
  '50000000-0000-0000-0000-000000000092',
  '10000000-0000-0000-0000-000000000001',
  'database_test',
  'command-clarification-reply',
  'message',
  'processing',
  '{}'::jsonb,
  null
);

select is(
  public.continue_command_clarification(
    (select request_id from command_clarification_fixture),
    '50000000-0000-0000-0000-000000000092',
    '11000000-0000-0000-0000-000000000001',
    'Maybe',
    'Should stock be added or removed?',
    (select extraction from public.command_clarification_requests
      where id = (select request_id from command_clarification_fixture))
  ),
  (select request_id from command_clarification_fixture),
  'an unresolved natural reply advances the same request'
);
select is(
  (
    select jsonb_array_length(clarification_replies)
    from public.command_clarification_requests
    where id = (select request_id from command_clarification_fixture)
  ),
  1,
  'clarification replies are retained'
);

insert into public.transaction_proposals (
  id,
  organization_id,
  location_id,
  source_event_id,
  created_by,
  intent,
  status,
  idempotency_key,
  raw_command,
  model_name,
  model_response_id,
  prompt_version
)
values (
  '40000000-0000-0000-0000-000000000091',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000092',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'pending_confirmation',
  'database-test-command-clarification',
  '{}'::jsonb,
  'gpt-test',
  'response-resolved',
  'inventory-command-clarification-v1'
);

select is(
  public.resolve_command_clarification(
    (select request_id from command_clarification_fixture),
    '50000000-0000-0000-0000-000000000092',
    '11000000-0000-0000-0000-000000000001',
    'Yes, all received stock',
    jsonb_set(
      jsonb_set(
        (select extraction from public.command_clarification_requests
          where id = (select request_id from command_clarification_fixture)),
        '{command,intent}',
        '"RECEIVE_STOCK"'::jsonb
      ),
      '{command,needs_clarification}',
      'false'::jsonb
    ),
    '40000000-0000-0000-0000-000000000091'
  ),
  (select request_id from command_clarification_fixture),
  'a resolved reply links the resulting proposal'
);
select is(
  (
    select status::text
    from public.command_clarification_requests
    where id = (select request_id from command_clarification_fixture)
  ),
  'resolved',
  'resolved clarification is no longer pending'
);
select is(
  (
    select proposal_id
    from public.command_clarification_requests
    where id = (select request_id from command_clarification_fixture)
  ),
  '40000000-0000-0000-0000-000000000091'::uuid,
  'the resumed proposal is retained for audit'
);
select is(
  public.find_pending_command_clarification(
    '11000000-0000-0000-0000-000000000001',
    123
  ),
  null::uuid,
  'resolved clarification no longer intercepts later chat'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  status,
  payload,
  processed_at
)
values
(
  '50000000-0000-0000-0000-000000000093',
  '10000000-0000-0000-0000-000000000001',
  'database_test',
  'command-clarification-cancel-source',
  'invoice_image',
  'processed',
  '{}'::jsonb,
  now()
),
(
  '50000000-0000-0000-0000-000000000094',
  '10000000-0000-0000-0000-000000000001',
  'database_test',
  'command-clarification-cancel-reply',
  'message',
  'processing',
  '{}'::jsonb,
  null
);

create temporary table cancelled_command_clarification_fixture as
select public.begin_command_clarification(
  '50000000-0000-0000-0000-000000000093',
  '11000000-0000-0000-0000-000000000001',
  124,
  'Should this image be recorded as received stock?',
  (select extraction
   from public.command_clarification_requests
   where id = (select request_id from command_clarification_fixture))
) as request_id;

select ok(
  (select request_id is not null from cancelled_command_clarification_fixture),
  'a second clarification can be created for cancellation'
);
select is(
  public.cancel_command_clarification(
    (select request_id from cancelled_command_clarification_fixture),
    '50000000-0000-0000-0000-000000000094',
    '11000000-0000-0000-0000-000000000001'
  ),
  (select request_id from cancelled_command_clarification_fixture),
  'the requesting actor can abandon a pending clarification'
);
select is(
  (
    select status::text
    from public.command_clarification_requests
    where id = (select request_id from cancelled_command_clarification_fixture)
  ),
  'cancelled',
  'abandoning a clarification records a cancelled terminal state'
);
select is(
  public.find_pending_command_clarification(
    '11000000-0000-0000-0000-000000000001',
    124
  ),
  null::uuid,
  'a cancelled clarification no longer intercepts later chat'
);

select * from finish();

rollback;
