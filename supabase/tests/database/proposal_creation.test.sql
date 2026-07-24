begin;

create extension if not exists pgtap with schema extensions;

select plan(17);

select has_function(
  'public',
  'create_inventory_proposal',
  array[
    'uuid', 'uuid', 'uuid', 'uuid', 'proposal_intent', 'text', 'jsonb',
    'text', 'text', 'text', 'text', 'jsonb'
  ],
  'atomic proposal creation function exists'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type
)
values (
  '50000000-0000-0000-0000-000000000001',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'proposal-test-update',
  'message'
);

select lives_ok(
  $$
    select public.create_inventory_proposal(
      '10000000-0000-0000-0000-000000000001',
      '12000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001',
      'receive_stock',
      'proposal-test-receive-cartons',
      '{"intent":"RECEIVE_STOCK"}',
      'gpt-test',
      'resp-test',
      'prompt-v1',
      null,
      '[{
        "line_number": 1,
        "source_text": "received two cartons of butter",
        "extracted_description": "Anchor butter",
        "requested_quantity": 2,
        "requested_unit": "carton",
        "item_variant_id": "21000000-0000-0000-0000-000000000001",
        "match_method": "exact_identifier",
        "match_score": 1,
        "match_evidence": {"source":"test"},
        "attributes": {}
      }]'
    )
  $$,
  'a resolved proposal is created'
);

select is(
  (
    select line.base_quantity_delta
    from public.proposal_lines as line
    join public.transaction_proposals as proposal on proposal.id = line.proposal_id
    where proposal.idempotency_key = 'proposal-test-receive-cartons'
  ),
  48::numeric,
  'unit conversion derives the base-unit receipt delta'
);

select is(
  (
    select line.base_unit
    from public.proposal_lines as line
    join public.transaction_proposals as proposal on proposal.id = line.proposal_id
    where proposal.idempotency_key = 'proposal-test-receive-cartons'
  ),
  'each',
  'resolved lines store the item base unit'
);

select lives_ok(
  $$
    select public.create_inventory_proposal(
      '10000000-0000-0000-0000-000000000001',
      '12000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001',
      'receive_stock',
      'proposal-test-generic-units',
      '{"intent":"RECEIVE_STOCK"}',
      'gpt-test',
      'resp-test-generic-units',
      'prompt-v1',
      null,
      '[{
        "line_number": 1,
        "source_text": "received three units of MILK-FULLCREAM-1L",
        "requested_quantity": 3,
        "requested_unit": "units",
        "item_variant_id": "21000000-0000-0000-0000-000000000002",
        "match_method": "exact_identifier",
        "match_score": 1,
        "match_evidence": {"source":"test"},
        "attributes": {}
      }]'
    )
  $$,
  'generic units resolve to one matched SKU unit each'
);

select is(
  (
    select line.base_quantity_delta
    from public.proposal_lines as line
    join public.transaction_proposals as proposal on proposal.id = line.proposal_id
    where proposal.idempotency_key = 'proposal-test-generic-units'
  ),
  3::numeric,
  'generic units derive a factor-one base-unit delta'
);

select lives_ok(
  $$
    select public.create_inventory_proposal(
      '10000000-0000-0000-0000-000000000001',
      '12000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001',
      'receive_stock',
      'proposal-test-receive-cartons',
      '{}'::jsonb,
      null, null, null, null,
      '[{"line_number":1,"source_text":"retry","requested_quantity":99}]'
    )
  $$,
  'repeated proposal creation is idempotent'
);

select is(
  (
    select count(*) from public.transaction_proposals
    where idempotency_key = 'proposal-test-receive-cartons'
  ),
  1::bigint,
  'idempotent creation retains one proposal'
);

select lives_ok(
  $$
    select public.create_inventory_proposal(
      '10000000-0000-0000-0000-000000000001',
      '12000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001',
      'receive_stock',
      'proposal-test-unresolved',
      '{}'::jsonb,
      null, null, null, null,
      '[{
        "line_number":1,
        "source_text":"received three mystery widgets",
        "requested_quantity":3,
        "match_evidence":{"candidates":[]}
      }]'
    )
  $$,
  'an ambiguous line can be persisted unresolved'
);

select is(
  (
    select line.base_quantity_delta
    from public.proposal_lines as line
    join public.transaction_proposals as proposal on proposal.id = line.proposal_id
    where proposal.idempotency_key = 'proposal-test-unresolved'
  ),
  null::numeric,
  'unresolved lines do not invent a stock delta'
);

update public.proposal_lines as line
set match_evidence = jsonb_build_object(
  'candidates', jsonb_build_array(
    jsonb_build_object(
      'item_variant_id', '21000000-0000-0000-0000-000000000002',
      'label', 'Full Cream Milk 1L'
    )
  )
)
from public.transaction_proposals as proposal
where proposal.id = line.proposal_id
  and proposal.idempotency_key = 'proposal-test-unresolved';

select lives_ok(
  format(
    'select public.resolve_proposal_line(%L, %L, %L)',
    (
      select line.id from public.proposal_lines as line
      join public.transaction_proposals as proposal on proposal.id = line.proposal_id
      where proposal.idempotency_key = 'proposal-test-unresolved'
    ),
    '21000000-0000-0000-0000-000000000002',
    '11000000-0000-0000-0000-000000000001'
  ),
  'a user can select an offered simple variant'
);

select is(
  (
    select line.match_method::text from public.proposal_lines as line
    join public.transaction_proposals as proposal on proposal.id = line.proposal_id
    where proposal.idempotency_key = 'proposal-test-unresolved'
  ),
  'human_selected',
  'human selection is retained as matching evidence'
);

select is(
  (
    select line.base_quantity_delta from public.proposal_lines as line
    join public.transaction_proposals as proposal on proposal.id = line.proposal_id
    where proposal.idempotency_key = 'proposal-test-unresolved'
  ),
  3::numeric,
  'human selection derives the base-unit delta'
);

select lives_ok(
  format(
    'select public.cancel_inventory_proposal(%L, %L)',
    (
      select proposal.id from public.transaction_proposals as proposal
      where proposal.idempotency_key = 'proposal-test-unresolved'
    ),
    '11000000-0000-0000-0000-000000000001'
  ),
  'a pending proposal can be cancelled'
);

select is(
  (
    select proposal.status::text from public.transaction_proposals as proposal
    where proposal.idempotency_key = 'proposal-test-unresolved'
  ),
  'rejected',
  'cancelled proposal is retained as rejected'
);

select throws_like(
  $$
    select public.create_inventory_proposal(
      '10000000-0000-0000-0000-000000000001',
      '12000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001',
      'receive_stock',
      'proposal-test-missing-match-method',
      '{}'::jsonb,
      null, null, 'inventory-agent-spike-v3', null,
      '[{
        "line_number": 1,
        "source_text": "received one butter",
        "requested_quantity": 1,
        "requested_unit": "each",
        "item_variant_id": "21000000-0000-0000-0000-000000000001",
        "match_method": null,
        "match_score": null,
        "match_evidence": {"source":"test"},
        "attributes": {}
      }]'
    )
  $$,
  '%proposal_lines_resolved_match_method_check%',
  'resolved proposals without a grounding method are rejected during creation'
);

select throws_like(
  $$
    select public.create_inventory_proposal(
      '10000000-0000-0000-0000-000000000001',
      '12000000-0000-0000-0000-000000000001',
      '50000000-0000-0000-0000-000000000001',
      '11000000-0000-0000-0000-000000000001',
      'adjust_stock',
      'proposal-test-adjust',
      '{}'::jsonb,
      null, null, null, null,
      '[{"line_number":1,"source_text":"set to three","requested_quantity":3}]'
    )
  $$,
  '%explicit adjustment mode%',
  'ambiguous stock adjustments are rejected until their semantics are explicit'
);

select * from finish();

rollback;
