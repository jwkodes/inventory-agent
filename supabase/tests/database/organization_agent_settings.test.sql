begin;

create extension if not exists pgtap with schema extensions;

select plan(12);

select has_table(
  'public',
  'organization_setting_changes',
  'organization setting changes have an audit table'
);
select has_function(
  'public',
  'load_organization_agent_context_settings',
  array['uuid'],
  'organization context settings loader exists'
);
select has_function(
  'public',
  'set_organization_agent_context_settings',
  array['uuid', 'text', 'integer', 'integer', 'integer', 'text'],
  'organization context settings writer exists'
);
select has_function(
  'public',
  'clear_organization_agent_context_settings',
  array['uuid', 'text'],
  'organization context settings reset exists'
);

select is(
  public.load_organization_agent_context_settings(
    '10000000-0000-0000-0000-000000000001'
  ),
  null,
  'organization initially inherits application defaults'
);

select is(
  public.set_organization_agent_context_settings(
    '10000000-0000-0000-0000-000000000001',
    'discard',
    5,
    12000,
    250,
    'dashboard:test'
  ),
  '{"policy":"discard","retention_days":5,"max_tokens":12000,"max_items":250}'::jsonb,
  'validated organization override is stored'
);

select is(
  public.load_organization_agent_context_settings(
    '10000000-0000-0000-0000-000000000001'
  ),
  '{"policy":"discard","retention_days":5,"max_tokens":12000,"max_items":250}'::jsonb,
  'stored organization override can be loaded'
);

select is(
  (
    select change.changed_by
    from public.organization_setting_changes as change
    where change.organization_id = '10000000-0000-0000-0000-000000000001'
    order by change.created_at desc
    limit 1
  ),
  'dashboard:test',
  'setting change actor is audited'
);

select throws_ok(
  $$
    select public.set_organization_agent_context_settings(
      '10000000-0000-0000-0000-000000000001',
      'invalid',
      5,
      12000,
      250,
      'dashboard:test'
    )
  $$,
  '22023',
  'Context policy must be discard or summarize',
  'invalid context policy is rejected'
);

select throws_ok(
  $$
    select public.set_organization_agent_context_settings(
      '10000000-0000-0000-0000-000000000001',
      'summarize',
      5,
      12000,
      351,
      'dashboard:test'
    )
  $$,
  '22023',
  'Context item limit must be between 1 and 350',
  'database item ceiling is enforced'
);

select is(
  public.clear_organization_agent_context_settings(
    '10000000-0000-0000-0000-000000000001',
    'dashboard:test'
  ),
  '{"policy":"discard","retention_days":5,"max_tokens":12000,"max_items":250}'::jsonb,
  'reset returns the removed override'
);

select is(
  public.load_organization_agent_context_settings(
    '10000000-0000-0000-0000-000000000001'
  ),
  null,
  'reset restores application defaults'
);

select * from finish();
rollback;
