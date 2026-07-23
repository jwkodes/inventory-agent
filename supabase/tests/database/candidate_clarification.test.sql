begin;

create extension if not exists pgtap with schema extensions;

select plan(18);

select has_table(
  'public',
  'match_clarification_requests',
  'durable match clarification state exists'
);
select has_function(
  'public', 'get_inventory_candidate_context', array['uuid', 'uuid[]'],
  'candidate attribute context function exists'
);
select has_function(
  'public', 'begin_match_clarifications', array['uuid', 'uuid', 'bigint'],
  'clarification begin function exists'
);
select has_function(
  'public', 'find_pending_match_clarification', array['uuid', 'bigint'],
  'pending clarification lookup exists'
);
select has_function(
  'public', 'get_match_clarification_view', array['uuid'],
  'clarification view function exists'
);
select has_function(
  'public',
  'apply_match_clarification_judgment',
  array['uuid', 'uuid', 'uuid', 'text', 'text', 'uuid', 'text', 'text', 'jsonb'],
  'clarification turn function exists'
);

select is(
  (
    select context.variant_attributes ->> 'colour'
    from public.get_inventory_candidate_context(
      '10000000-0000-0000-0000-000000000001',
      array['21000000-0000-0000-0000-000000000004'::uuid]
    ) as context
  ),
  'red',
  'candidate context includes variant colour'
);
select is(
  (
    select context.attribute_matching_roles ->> 'colour'
    from public.get_inventory_candidate_context(
      '10000000-0000-0000-0000-000000000001',
      array['21000000-0000-0000-0000-000000000004'::uuid]
    ) as context
  ),
  'discriminator',
  'company field configuration marks colour as a discriminator'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  status,
  processed_at,
  payload
)
values (
  '50000000-0000-0000-0000-000000000401',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'candidate-clarification-source',
  'message',
  'processed',
  now(),
  '{"message":{"from":{"id":100000001},"chat":{"id":100000001},"text":"received four shirts"}}'
);

create temporary table clarification_proposal as
select public.create_inventory_proposal(
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000401',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'candidate-clarification-proposal',
  '{
    "intent":"RECEIVE_STOCK",
    "lines":[{
      "source_text":"four classic t-shirts",
      "item_reference":{"type":"NAME","value":"classic t-shirt"},
      "description":"classic t-shirt",
      "quantity":"4",
      "unit":"each",
      "attributes":[]
    }]
  }'::jsonb,
  'gpt-test',
  'candidate-clarification-response',
  'prompt-v1',
  null,
  jsonb_build_array(
    jsonb_build_object(
      'line_number', 1,
      'source_text', 'four classic t-shirts',
      'extracted_description', 'classic t-shirt',
      'requested_quantity', 4,
      'requested_unit', 'each',
      'match_evidence', jsonb_build_object(
        'decision', 'clarification_required',
        'reason', 'Colour is missing',
        'clarification_question', 'Which colour is it?',
        'candidates', jsonb_build_array(
          jsonb_build_object(
            'item_variant_id', '21000000-0000-0000-0000-000000000004',
            'item_id', '20000000-0000-0000-0000-000000000004',
            'item_name', 'Classic T-Shirt',
            'variant_name', 'Classic T-Shirt - Red / M',
            'sku', 'SHIRT-RED-M',
            'base_unit', 'each',
            'tracking_mode', 'simple',
            'match_method', 'semantic_rerank',
            'match_score', 0.88,
            'match_evidence', '{}'::jsonb
          ),
          jsonb_build_object(
            'item_variant_id', '21000000-0000-0000-0000-000000000005',
            'item_id', '20000000-0000-0000-0000-000000000004',
            'item_name', 'Classic T-Shirt',
            'variant_name', 'Classic T-Shirt - Blue / L',
            'sku', 'SHIRT-BLUE-L',
            'base_unit', 'each',
            'tracking_mode', 'simple',
            'match_method', 'semantic_rerank',
            'match_score', 0.86,
            'match_evidence', '{}'::jsonb
          )
        )
      ),
      'attributes', '{}'::jsonb
    )
  )
) as proposal_id;

select is(
  public.begin_match_clarifications(
    (select proposal_id from clarification_proposal),
    '11000000-0000-0000-0000-000000000001',
    100000001
  ),
  1,
  'one unresolved line starts one clarification'
);

create temporary table clarification_request as
select public.find_pending_match_clarification(
  '11000000-0000-0000-0000-000000000001',
  100000001
) as request_id;

select isnt(
  (select request_id from clarification_request),
  null::uuid,
  'pending clarification is routed by actor and chat'
);
select is(
  public.get_match_clarification_view(
    (select request_id from clarification_request)
  ) ->> 'question',
  'Which colour is it?',
  'clarification view returns the focused question'
);
select is(
  jsonb_array_length(
    public.get_match_clarification_view(
      (select request_id from clarification_request)
    ) -> 'candidates'
  ),
  2,
  'clarification view retains only the offered candidates'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type, status, payload
)
values
  (
    '50000000-0000-0000-0000-000000000402',
    '10000000-0000-0000-0000-000000000001',
    'telegram',
    'candidate-clarification-red',
    'message',
    'processing',
    '{"message":{"text":"red"}}'
  ),
  (
    '50000000-0000-0000-0000-000000000403',
    '10000000-0000-0000-0000-000000000001',
    'telegram',
    'candidate-clarification-medium',
    'message',
    'processing',
    '{"message":{"text":"medium"}}'
  );

select is(
  public.apply_match_clarification_judgment(
    (select request_id from clarification_request),
    '50000000-0000-0000-0000-000000000402',
    '11000000-0000-0000-0000-000000000001',
    'red',
    'ASK_USER',
    null,
    'What size is it?',
    'Colour is known but size is still missing.',
    '{"colour":"red"}'::jsonb
  ),
  (select proposal_id from clarification_proposal),
  'an answer can lead to another focused question'
);
select is(
  (
    select request.question
    from public.match_clarification_requests as request
    where request.id = (select request_id from clarification_request)
  ),
  'What size is it?',
  'follow-up question is durable'
);
select is(
  (
    select line.attributes ->> 'colour'
    from public.proposal_lines as line
    where line.proposal_id = (select proposal_id from clarification_proposal)
  ),
  'red',
  'facts learned during the conversation are retained'
);

select is(
  public.apply_match_clarification_judgment(
    (select request_id from clarification_request),
    '50000000-0000-0000-0000-000000000403',
    '11000000-0000-0000-0000-000000000001',
    'medium',
    'SELECT',
    '21000000-0000-0000-0000-000000000004',
    null,
    'Red and medium identify the offered red medium variant.',
    '{"size":"M"}'::jsonb
  ),
  (select proposal_id from clarification_proposal),
  'a later answer resolves the offered variant'
);
select is(
  (
    select line.item_variant_id
    from public.proposal_lines as line
    where line.proposal_id = (select proposal_id from clarification_proposal)
  ),
  '21000000-0000-0000-0000-000000000004'::uuid,
  'resolved line points at the selected colour and size variant'
);
select is(
  (
    select request.status::text
    from public.match_clarification_requests as request
    where request.id = (select request_id from clarification_request)
  ),
  'resolved',
  'resolved conversation no longer captures future user messages'
);

select * from finish();
rollback;
