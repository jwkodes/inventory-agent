begin;

create extension if not exists pgtap with schema extensions;

select plan(16);

select has_table(
  'public',
  'inventory_agent_turns',
  'immutable inventory agent turns exist'
);
select has_column(
  'public',
  'inventory_agent_conversations',
  'summary',
  'agent conversations can retain a rolling summary'
);
select has_function(
  'public',
  'save_inventory_agent_conversation_turn',
  array[
    'uuid', 'uuid', 'uuid', 'jsonb', 'jsonb', 'integer', 'uuid[]', 'uuid[]',
    'text', 'uuid', 'uuid', 'text', 'text', 'text', 'integer', 'integer', 'integer',
    'integer', 'integer'
  ],
  'timestamped agent turn save function exists'
);
select has_function(
  'public',
  'compact_inventory_agent_conversation',
  array['uuid', 'uuid', 'uuid[]', 'text', 'text'],
  'agent context compaction function exists'
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
  '50000000-0000-0000-0000-000000000602',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'agent-context-retention-source',
  'message',
  'processing',
  now(),
  1,
  '{"message":{"from":{"id":100000002},"chat":{"id":100000002},"text":"show milk"}}'
);

create temporary table retained_agent_conversation as
select public.load_inventory_agent_conversation(
  '10000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  100000002
) as view;

select is(
  public.save_inventory_agent_conversation_turn(
    (select (view ->> 'conversation_id')::uuid from retained_agent_conversation),
    '50000000-0000-0000-0000-000000000602',
    '11000000-0000-0000-0000-000000000001',
    '[{"role":"user","content":"show milk"},{"role":"assistant","content":"200 each"}]'::jsonb,
    '[{"role":"user","content":"show milk"},{"role":"assistant","content":"200 each"}]'::jsonb,
    20,
    array['21000000-0000-0000-0000-000000000001'::uuid],
    '{}'::uuid[],
    'There are 200 each of Full Cream Milk 1L.',
    null,
    null,
    null,
    'resp-context-test',
    'gpt-test',
    10,
    6,
    2,
    5,
    15
  ),
  (select (view ->> 'conversation_id')::uuid from retained_agent_conversation),
  'one immutable turn and active conversation are saved atomically'
);

create temporary table loaded_agent_context as
select public.load_inventory_agent_conversation(
  '10000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  100000002
) as view;

select is(
  jsonb_array_length((select view -> 'active_turns' from loaded_agent_context)),
  1,
  'saved turn is returned as active context'
);
select is(
  (
    select view -> 'active_turns' -> 0 ->> 'source_event_id'
    from loaded_agent_context
  ),
  '50000000-0000-0000-0000-000000000602',
  'active turn retains its source event'
);
select is(
  (
    select turn.total_tokens
    from public.inventory_agent_turns as turn
    where turn.source_event_id = '50000000-0000-0000-0000-000000000602'
  ),
  15,
  'turn audit row retains provider token usage'
);
select is(
  (
    select turn.cached_input_tokens
    from public.inventory_agent_turns as turn
    where turn.source_event_id = '50000000-0000-0000-0000-000000000602'
  ),
  6,
  'turn audit row retains prompt-cache read usage'
);
select is(
  (
    select turn.cache_write_tokens
    from public.inventory_agent_turns as turn
    where turn.source_event_id = '50000000-0000-0000-0000-000000000602'
  ),
  2,
  'turn audit row retains prompt-cache write usage'
);

select is(
  public.compact_inventory_agent_conversation(
    (select (view ->> 'conversation_id')::uuid from retained_agent_conversation),
    '11000000-0000-0000-0000-000000000001',
    array[
      (
        select (view -> 'active_turns' -> 0 ->> 'turn_id')::uuid
        from loaded_agent_context
      )
    ],
    'summarize',
    'The user previously asked about milk.'
  ),
  (select (view ->> 'conversation_id')::uuid from retained_agent_conversation),
  'active context can be compacted'
);

select is(
  (
    select jsonb_array_length(conversation.history)
    from public.inventory_agent_conversations as conversation
    where conversation.id = (
      select (view ->> 'conversation_id')::uuid from retained_agent_conversation
    )
  ),
  0,
  'compacted turn leaves active model history'
);
select is(
  (
    select conversation.summary
    from public.inventory_agent_conversations as conversation
    where conversation.id = (
      select (view ->> 'conversation_id')::uuid from retained_agent_conversation
    )
  ),
  'The user previously asked about milk.',
  'rolling summary is stored on the conversation'
);
select is(
  (
    select cardinality(conversation.allowed_variant_ids)
    from public.inventory_agent_conversations as conversation
    where conversation.id = (
      select (view ->> 'conversation_id')::uuid from retained_agent_conversation
    )
  ),
  0,
  'compaction clears stale grounded IDs'
);
select is(
  (
    select count(*)
    from public.inventory_agent_turns as turn
    where turn.source_event_id = '50000000-0000-0000-0000-000000000602'
  ),
  1::bigint,
  'compaction retains the immutable raw turn'
);
select is(
  (
    select turn.compaction_policy
    from public.inventory_agent_turns as turn
    where turn.source_event_id = '50000000-0000-0000-0000-000000000602'
  ),
  'summarize',
  'raw turn records why it left active context'
);

select * from finish();
rollback;
