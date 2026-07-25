begin;

create extension if not exists pgtap with schema extensions;

update public.organization_users
set telegram_user_id = 100000001
where id = '11000000-0000-0000-0000-000000000001';

select plan(40);

select has_table(
  'public',
  'catalog_item_creation_requests',
  'durable catalog item creation requests exist'
);
select has_function(
  'public', 'browse_inventory_candidates', array['uuid', 'text', 'integer'],
  'fallback catalog browsing function exists'
);
select has_function(
  'public', 'show_existing_inventory_candidates', array['uuid', 'uuid'],
  'show-existing action exists'
);
select has_function(
  'public', 'begin_catalog_item_creation', array['uuid', 'uuid', 'bigint'],
  'catalog item creation begin action exists'
);
select has_function(
  'public', 'catalog_item_name_suggestion', array['uuid'],
  'catalog item name fallback exists'
);
select has_function(
  'public', 'find_pending_catalog_item_creation', array['uuid', 'bigint'],
  'pending catalog form lookup exists'
);
select has_function(
  'public',
  'save_catalog_item_creation_details',
  array['uuid', 'uuid', 'uuid', 'text', 'text', 'text', 'text', 'jsonb'],
  'catalog detail capture exists'
);
select has_function(
  'public',
  'save_catalog_item_creation_draft',
  array['uuid', 'uuid', 'uuid', 'text', 'text', 'text', 'text', 'jsonb'],
  'partial catalog detail capture exists'
);
select has_function(
  'public', 'confirm_catalog_item_creation', array['uuid', 'uuid'],
  'catalog item confirmation exists'
);
select has_function(
  'public', 'prepare_catalog_item_creation_confirmation', array['uuid', 'uuid'],
  'catalog item confirmation validates SKU availability'
);
select has_function(
  'public', 'cancel_catalog_item_creation', array['uuid', 'uuid'],
  'catalog item cancellation exists'
);

create temporary table fallback_candidates as
select
  row_number() over () as position,
  candidate.*
from public.browse_inventory_candidates(
  '10000000-0000-0000-0000-000000000001',
  'purple widget PGTAP-ZX-999',
  5
) as candidate;

select ok(
  (select count(*) between 1 and 5 from fallback_candidates),
  'fallback browsing returns up to the requested number of catalog items'
);
select ok(
  not exists (
    select 1
    from fallback_candidates as current
    join fallback_candidates as following
      on following.position = current.position + 1
    where current.match_score < following.match_score
  ),
  'fallback candidate scores are descending'
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
  '50000000-0000-0000-0000-000000000300',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'catalog-resolution-source',
  'message',
  'processed',
  now(),
  '{"message":{"from":{"id":100000001},"chat":{"id":100000001},"text":"received four purple widgets"}}'
);

create temporary table catalog_proposal as
select public.create_inventory_proposal(
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000300',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'catalog-resolution-proposal',
  '{
    "intent":"RECEIVE_STOCK",
    "lines":[{
      "source_text":"4 units of purple widget PGTAP-ZX-999",
      "item_reference":{"type":"PART_NUMBER","value":"PGTAP-ZX-999"},
      "description":"Purple Widget",
      "quantity":"4",
      "unit":"units",
      "attributes":[]
    }]
  }'::jsonb,
  'gpt-test',
  'catalog-response',
  'prompt-v1',
  null,
  jsonb_build_array(
    jsonb_build_object(
      'line_number', 1,
      'source_text', '4 units of purple widget PGTAP-ZX-999',
      'extracted_description', 'Purple Widget',
      'requested_quantity', 4,
      'requested_unit', 'units',
      'match_evidence', jsonb_build_object(
        'decision', 'not_found',
        'reason', 'No candidate met threshold',
        'candidates', (
          select jsonb_agg(to_jsonb(candidate) order by candidate.position)
          from fallback_candidates as candidate
        )
      ),
      'attributes', '{}'::jsonb
    )
  )
) as proposal_id;

create temporary table catalog_line as
select line.id as line_id
from public.proposal_lines as line
where line.proposal_id = (select proposal_id from catalog_proposal);

select is(
  public.catalog_item_name_suggestion((select line_id from catalog_line)),
  'Purple Widget',
  'catalog name prefers the quantity-free extracted description'
);

select is(
  public.show_existing_inventory_candidates(
    (select line_id from catalog_line),
    '11000000-0000-0000-0000-000000000001'
  ),
  (select proposal_id from catalog_proposal),
  'user can request the low-confidence existing-item list'
);
select is(
  (
    select (line.match_evidence ->> 'show_candidates')::boolean
    from public.proposal_lines as line
    where line.id = (select line_id from catalog_line)
  ),
  true,
  'show-existing preference is retained on the proposal line'
);

create temporary table catalog_request as
select public.begin_catalog_item_creation(
  (select line_id from catalog_line),
  '11000000-0000-0000-0000-000000000001',
  100000001
) as request_id;

select is(
  (
    select request.status::text
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from catalog_request)
  ),
  'awaiting_details',
  'Add new item starts durable detail collection'
);
select is(
  (
    select request.suggested_sku
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from catalog_request)
  ),
  'PGTAP-ZX-999',
  'part number is suggested as the new SKU'
);
select is(
  public.find_pending_catalog_item_creation(
    '11000000-0000-0000-0000-000000000001',
    100000001
  ),
  (select request_id from catalog_request),
  'pending detail form is routed by actor and chat'
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
  '50000000-0000-0000-0000-000000000301',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'catalog-details-source',
  'message',
  'processing',
  now(),
  1,
  '{"message":{"from":{"id":100000001},"chat":{"id":100000001},"text":"catalog details"}}'
);

select is(
  public.save_catalog_item_creation_draft(
    (select request_id from catalog_request),
    '50000000-0000-0000-0000-000000000301',
    '11000000-0000-0000-0000-000000000001',
    'Purple Widget',
    null,
    'each',
    null,
    '{"colour":"purple"}'::jsonb
  ),
  (select request_id from catalog_request),
  'partial natural-language details are retained'
);
select is(
  (
    select request.attributes ->> 'colour'
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from catalog_request)
  ),
  'purple',
  'partial attributes survive until the next clarification turn'
);

select is(
  public.save_catalog_item_creation_details(
    (select request_id from catalog_request),
    '50000000-0000-0000-0000-000000000301',
    '11000000-0000-0000-0000-000000000001',
    'Purple Widget',
    'PGTAP-ZX-999',
    'each',
    'simple',
    '{"colour":"purple"}'::jsonb
  ),
  (select request_id from catalog_request),
  'submitted item details are retained for review'
);
select is(
  (
    select request.status::text
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from catalog_request)
  ),
  'awaiting_confirmation',
  'valid details advance to final catalog confirmation'
);
select is(
  public.get_catalog_item_creation_view(
    (select request_id from catalog_request)
  ) ->> 'sku',
  'PGTAP-ZX-999',
  'confirmation view returns submitted details'
);

select is(
  public.confirm_catalog_item_creation(
    (select request_id from catalog_request),
    '11000000-0000-0000-0000-000000000001'
  ),
  (select proposal_id from catalog_proposal),
  'manager confirmation resumes the original proposal'
);
select is(
  (
    select variant.sku
    from public.item_variants as variant
    join public.catalog_item_creation_requests as request
      on request.created_variant_id = variant.id
    where request.id = (select request_id from catalog_request)
  ),
  'PGTAP-ZX-999',
  'confirmation creates the catalog variant'
);
select is(
  (
    select line.base_quantity_delta
    from public.proposal_lines as line
    where line.id = (select line_id from catalog_line)
  ),
  4::numeric,
  'created variant resolves the original proposal quantity'
);
select is(
  (
    select request.status::text
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from catalog_request)
  ),
  'completed',
  'catalog creation request records completion'
);
select lives_ok(
  $$
    select public.apply_inventory_proposal(
      (select proposal_id from catalog_proposal),
      '11000000-0000-0000-0000-000000000001'
    )
  $$,
  'resumed proposal can be applied normally'
);
select is(
  (
    select balance.quantity
    from public.inventory_balances as balance
    join public.item_variants as variant on variant.id = balance.item_variant_id
    where variant.sku = 'PGTAP-ZX-999'
  ),
  4::numeric,
  'new item receives the original stock quantity'
);
select is(
  public.confirm_catalog_item_creation(
    (select request_id from catalog_request),
    '11000000-0000-0000-0000-000000000001'
  ),
  (select proposal_id from catalog_proposal),
  'catalog confirmation is idempotent'
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
  '50000000-0000-0000-0000-000000000302',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'agent-catalog-draft-source',
  'message',
  'processed',
  now(),
  '{"message":{"from":{"id":100000001},"chat":{"id":100000001},"text":"PGTAP-AMOX-502 is new"}}'
);

create temporary table agent_catalog_proposal as
select public.create_inventory_proposal(
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000302',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'agent-catalog-draft-proposal',
  '{
    "intent":"RECEIVE_STOCK",
    "lines":[{
      "source_text":"3 boxes of PGTAP-AMOX-502",
      "item_reference":{"type":"SKU","value":"PGTAP-AMOX-502"},
      "description":"Amoxicillin",
      "quantity":"3",
      "unit":"box",
      "attributes":[{"key":"strength","value":"502mg"}]
    }]
  }'::jsonb,
  'gpt-test',
  'agent-catalog-response',
  'inventory-agent-spike-v4',
  null,
  jsonb_build_array(
    jsonb_build_object(
      'line_number', 1,
      'source_text', '3 boxes of PGTAP-AMOX-502',
      'extracted_description', 'Amoxicillin',
      'requested_quantity', 3,
      'requested_unit', 'box',
      'match_evidence', jsonb_build_object(
        'decision', 'not_found',
        'source', 'inventory_agent_tool',
        'new_item', jsonb_build_object(
          'name', 'Amoxicillin',
          'sku', 'PGTAP-AMOX-502',
          'base_unit', 'box',
          'tracking_mode', 'simple',
          'attributes', jsonb_build_array(
            jsonb_build_object('key', 'strength', 'value', '502mg'),
            jsonb_build_object('key', 'brand', 'value', 'Example Labs')
          )
        ),
        'candidates', '[]'::jsonb
      ),
      'attributes', '{"strength":"502mg"}'::jsonb
    )
  )
) as proposal_id;

create temporary table agent_catalog_line as
select line.id as line_id
from public.proposal_lines as line
where line.proposal_id = (select proposal_id from agent_catalog_proposal);

create temporary table agent_catalog_request as
select public.begin_catalog_item_creation(
  (select line_id from agent_catalog_line),
  '11000000-0000-0000-0000-000000000001',
  100000001
) as request_id;

select is(
  (
    select request.status::text
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from agent_catalog_request)
  ),
  'awaiting_confirmation',
  'a complete agent catalog draft skips duplicate detail collection'
);
select is(
  (
    select request.name
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from agent_catalog_request)
  ),
  'Amoxicillin',
  'the user-provided agent item name is retained'
);
select is(
  (
    select request.attributes ->> 'strength'
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from agent_catalog_request)
  ),
  '502mg',
  'a user-provided optional strength attribute is retained'
);
select is(
  (
    select request.attributes ->> 'brand'
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from agent_catalog_request)
  ),
  'Example Labs',
  'multiple user-provided optional attributes are retained'
);
select is(
  public.find_pending_catalog_item_creation(
    '11000000-0000-0000-0000-000000000001',
    100000001
  ),
  null,
  'a complete agent draft waits for confirmation rather than another text reply'
);

update public.catalog_item_creation_requests
set sku = 'PGTAP-ZX-999'
where id = (select request_id from agent_catalog_request);

select is(
  (
    public.prepare_catalog_item_creation_confirmation(
      (select request_id from agent_catalog_request),
      '11000000-0000-0000-0000-000000000001'
    ) ->> 'ready'
  ),
  'false',
  'a duplicate SKU reopens catalog detail collection instead of failing silently'
);
select is(
  (
    select request.status::text
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from agent_catalog_request)
  ),
  'awaiting_details',
  'duplicate SKU recovery waits for a natural-language correction'
);
select is(
  (
    public.prepare_catalog_item_creation_confirmation(
      (select request_id from agent_catalog_request),
      '11000000-0000-0000-0000-000000000001'
    ) ->> 'ready'
  ),
  'false',
  'repeated stale button clicks return the same recoverable conflict'
);
select ok(
  (
    select request.sku is null
      and request.details_reason like '%PGTAP-ZX-999%'
    from public.catalog_item_creation_requests as request
    where request.id = (select request_id from agent_catalog_request)
  ),
  'duplicate SKU recovery clears the conflict and explains what must change'
);

select * from finish();
rollback;
