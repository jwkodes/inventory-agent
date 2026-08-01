update public.inventory_agent_conversations as conversation
set history = (
  select coalesce(
    jsonb_agg(item.value order by item.ordinality),
    '[]'::jsonb
  )
  from jsonb_array_elements(conversation.history)
    with ordinality as item(value, ordinality)
  where coalesce(item.value ->> 'type', '') <> 'reasoning'
)
where exists (
  select 1
  from jsonb_array_elements(conversation.history) as item(value)
  where item.value ->> 'type' = 'reasoning'
);

update public.inventory_agent_turns as turn
set estimated_tokens = greatest(
  1,
  ceil(
    length(
      (
        select coalesce(
          jsonb_agg(item.value order by item.ordinality),
          '[]'::jsonb
        )
        from jsonb_array_elements(turn.history)
          with ordinality as item(value, ordinality)
        where coalesce(item.value ->> 'type', '') <> 'reasoning'
      )::text
    ) / 4.0
  )::integer
)
where turn.compacted_at is null;

comment on column public.inventory_agent_turns.estimated_tokens is
  'Approximate active-context tokens excluding private reasoning items retained only for audit.';
