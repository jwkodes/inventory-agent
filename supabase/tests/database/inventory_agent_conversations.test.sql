begin;

create extension if not exists pgtap with schema extensions;

select plan(15);

select has_table(
  'public',
  'inventory_agent_conversations',
  'durable inventory agent conversations exist'
);
select has_function(
  'public',
  'load_inventory_agent_conversation',
  array['uuid', 'uuid', 'bigint'],
  'agent conversation load function exists'
);
select has_function(
  'public',
  'save_inventory_agent_conversation',
  array[
    'uuid', 'uuid', 'uuid', 'jsonb', 'uuid[]', 'uuid[]',
    'text', 'uuid', 'uuid', 'text', 'text', 'text'
  ],
  'agent conversation save function exists'
);
select has_function(
  'public',
  'get_inventory_agent_variant_balances',
  array['uuid', 'uuid', 'uuid[]'],
  'agent balance read function exists'
);
select has_function(
  'public',
  'read_inventory_agent_transactions',
  array['uuid', 'text', 'integer'],
  'agent transaction read function exists'
);
select ok(
  'agent_message' = any(enum_range(null::public.processing_outcome_type)::text[]),
  'agent messages have a durable outbox outcome type'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  status,
  processing_started_at,
  processing_attempts,
  payload
)
values (
  '50000000-0000-0000-0000-000000000601',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'agent-conversation-source',
  'message',
  'processing',
  now(),
  1,
  '{"message":{"from":{"id":100000001},"chat":{"id":100000001},"text":"show butter"}}'
);

create temporary table agent_conversation as
select public.load_inventory_agent_conversation(
  '10000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  100000001
) as view;

select isnt(
  (select (view ->> 'conversation_id')::uuid from agent_conversation),
  null::uuid,
  'loading creates a durable conversation ID'
);
select is(
  jsonb_array_length((select view -> 'history' from agent_conversation)),
  0,
  'new conversations start with empty history'
);

select is(
  public.save_inventory_agent_conversation(
    (select (view ->> 'conversation_id')::uuid from agent_conversation),
    '50000000-0000-0000-0000-000000000601',
    '11000000-0000-0000-0000-000000000001',
    '[{"role":"user","content":"show butter"},{"role":"assistant","content":"120 each"}]'::jsonb,
    array['21000000-0000-0000-0000-000000000001'::uuid],
    '{}'::uuid[],
    'There are 120 each of Anchor Butter 500g.',
    null,
    null,
    null,
    'resp-agent-test',
    'gpt-test'
  ),
  (select (view ->> 'conversation_id')::uuid from agent_conversation),
  'one grounded turn is persisted'
);
select is(
  (
    select conversation.last_source_event_id
    from public.inventory_agent_conversations as conversation
    where conversation.id = (
      select (view ->> 'conversation_id')::uuid from agent_conversation
    )
  ),
  '50000000-0000-0000-0000-000000000601'::uuid,
  'saved conversation retains replay source event'
);
select is(
  (
    select cardinality(conversation.allowed_variant_ids)
    from public.inventory_agent_conversations as conversation
    where conversation.id = (
      select (view ->> 'conversation_id')::uuid from agent_conversation
    )
  ),
  1,
  'saved conversation retains grounded variant IDs'
);

select is(
  (
    select balance.on_hand
    from public.get_inventory_agent_variant_balances(
      '10000000-0000-0000-0000-000000000001',
      '12000000-0000-0000-0000-000000000001',
      array['21000000-0000-0000-0000-000000000001'::uuid]
    ) as balance
  ),
  120::numeric,
  'agent balance reads are location scoped'
);
select is(
  (
    select count(*)
    from public.read_inventory_agent_transactions(
      '10000000-0000-0000-0000-000000000001',
      null,
      5
    )
  ),
  0::bigint,
  'transaction reads safely return an empty history'
);

select throws_ok(
  $$
    select public.save_inventory_agent_conversation(
      (select (view ->> 'conversation_id')::uuid from agent_conversation),
      '50000000-0000-0000-0000-000000000601',
      '11000000-0000-0000-0000-000000000001',
      '[]'::jsonb,
      array['ffffffff-ffff-ffff-ffff-ffffffffffff'::uuid],
      '{}'::uuid[],
      'Invalid grounding',
      null,
      null,
      null,
      'resp-invalid',
      'gpt-test'
    )
  $$,
  '22023',
  'Conversation contains a cross-organization variant',
  'unknown or cross-organization variants cannot enter durable context'
);

select isnt(
  public.enqueue_processing_outcome(
    '10000000-0000-0000-0000-000000000001',
    '50000000-0000-0000-0000-000000000601',
    'agent_message',
    null,
    100000001,
    '{"message":"There are 120 each of Anchor Butter 500g."}'::jsonb
  ),
  null::uuid,
  'agent replies enter the existing durable Telegram outbox'
);

select * from finish();
rollback;
