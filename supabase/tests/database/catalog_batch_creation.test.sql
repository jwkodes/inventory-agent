begin;

create extension if not exists pgtap with schema extensions;

select plan(25);

select has_table(
  'public',
  'catalog_batch_creation_requests',
  'bulk catalog requests are durable'
);
select has_function(
  'public', 'begin_catalog_batch_creation', array['uuid', 'uuid', 'bigint'],
  'bulk catalog creation can begin from a proposal'
);
select has_function(
  'public', 'get_catalog_batch_creation_view', array['uuid'],
  'bulk catalog review projection exists'
);
select has_function(
  'public', 'save_catalog_batch_creation_draft', array['uuid', 'uuid', 'uuid', 'jsonb'],
  'one reply can save every bulk catalog draft'
);
select has_function(
  'public', 'confirm_catalog_batch_creation', array['uuid', 'uuid'],
  'bulk catalog confirmation exists'
);
select has_function(
  'public', 'confirm_catalog_batch_and_apply_inventory', array['uuid', 'uuid'],
  'catalog creation and stock application share one atomic confirmation'
);
select has_function(
  'public', 'cancel_catalog_batch_and_proposal', array['uuid', 'uuid'],
  'one cancellation rejects both the catalog batch and stock proposal'
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
  '50000000-0000-0000-0000-000000000720',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'catalog-batch-invoice',
  'invoice_image',
  'processed',
  now(),
  '{}'::jsonb
);

create temporary table batch_proposal as
select public.create_inventory_proposal(
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000720',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'catalog-batch-proposal',
  '{
    "intent":"RECEIVE_STOCK",
    "lines":[
      {
        "source_text":"2W-10 DC24V N/C",
        "item_reference":{"type":"NAME","value":"2W-10 DC24V N/C"},
        "description":"2W-10 DC24V N/C",
        "quantity":"1",
        "unit":"PCS",
        "attributes":[]
      },
      {
        "source_text":"2W-25 DC24V N/C",
        "item_reference":{"type":"NAME","value":"2W-25 DC24V N/C"},
        "description":"2W-25 DC24V N/C",
        "quantity":"4",
        "unit":"PCS",
        "attributes":[]
      }
    ]
  }'::jsonb,
  'gpt-test',
  'catalog-batch-response',
  'invoice-v1',
  null,
  '[
    {
      "line_number":1,
      "source_text":"2W-10 DC24V N/C",
      "extracted_description":"2W-10 DC24V N/C",
      "requested_quantity":1,
      "requested_unit":"PCS",
      "match_evidence":{"decision":"not_found"},
      "attributes":{}
    },
    {
      "line_number":2,
      "source_text":"2W-25 DC24V N/C",
      "extracted_description":"2W-25 DC24V N/C",
      "requested_quantity":4,
      "requested_unit":"PCS",
      "match_evidence":{"decision":"not_found"},
      "attributes":{}
    }
  ]'::jsonb
) as proposal_id;

create temporary table catalog_batch as
select public.begin_catalog_batch_creation(
  (select proposal_id from batch_proposal),
  '11000000-0000-0000-0000-000000000001',
  100000001
) as batch_id;

select is(
  (
    select status::text
    from public.catalog_batch_creation_requests
    where id = (select batch_id from catalog_batch)
  ),
  'awaiting_details',
  'the batch waits for missing identifiers'
);
select is(
  (
    select count(*)::integer
    from public.catalog_item_creation_requests
    where batch_id = (select batch_id from catalog_batch)
  ),
  2,
  'one child draft is retained for every unmatched line'
);
select is(
  public.find_pending_catalog_batch_creation(
    '11000000-0000-0000-0000-000000000001',
    100000001
  ),
  (select batch_id from catalog_batch),
  'the batch reply is routed by actor and chat'
);
select is(
  public.find_pending_catalog_item_creation(
    '11000000-0000-0000-0000-000000000001',
    100000001
  ),
  null::uuid,
  'batch children do not leak into the single-item reply flow'
);
select is(
  (
    public.get_catalog_batch_creation_view(
      (select batch_id from catalog_batch)
    ) #>> '{items,0,requested_quantity}'
  ),
  '1.00000000',
  'the first invoice quantity is retained in the review'
);
select is(
  (
    public.get_catalog_batch_creation_view(
      (select batch_id from catalog_batch)
    ) #>> '{items,1,requested_quantity}'
  ),
  '4.00000000',
  'the second invoice quantity is retained in the review'
);

insert into public.source_events (
  id,
  organization_id,
  provider,
  external_event_id,
  event_type,
  status,
  processing_started_at,
  payload
)
values (
  '50000000-0000-0000-0000-000000000721',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'catalog-batch-details',
  'message',
  'processing',
  now(),
  '{}'::jsonb
);

select lives_ok(
  format(
    $sql$
      select public.save_catalog_batch_creation_draft(
        %L::uuid,
        '50000000-0000-0000-0000-000000000721',
        '11000000-0000-0000-0000-000000000001',
        %L::jsonb
      )
    $sql$,
    (select batch_id from catalog_batch),
    (
      select jsonb_agg(
        jsonb_build_object(
          'request_id', request.id,
          'name', request.suggested_name,
          'sku', case line.line_number
            when 1 then 'PG-BULK-2W10'
            else 'PG-BULK-2W25'
          end,
          'base_unit', 'each',
          'tracking_mode', 'simple',
          'attributes', '{}'::jsonb
        )
        order by line.line_number
      )::text
      from public.catalog_item_creation_requests as request
      join public.proposal_lines as line on line.id = request.proposal_line_id
      where request.batch_id = (select batch_id from catalog_batch)
    )
  ),
  'one reply saves details for both products'
);
select is(
  (
    select status::text
    from public.catalog_batch_creation_requests
    where id = (select batch_id from catalog_batch)
  ),
  'awaiting_confirmation',
  'the complete batch advances to one confirmation'
);

create temporary table batch_confirmation as
select public.confirm_catalog_batch_creation(
  (select batch_id from catalog_batch),
  '11000000-0000-0000-0000-000000000001'
) as result;

select is(
  (select result ->> 'proposal_id' from batch_confirmation),
  (select proposal_id::text from batch_proposal),
  'one confirmation resumes the original proposal'
);
select is(
  (
    select count(*)::integer
    from public.proposal_lines
    where proposal_id = (select proposal_id from batch_proposal)
      and item_variant_id is not null
  ),
  2,
  'both proposal lines are resolved'
);
select is(
  (
    select sum(base_quantity_delta)
    from public.proposal_lines
    where proposal_id = (select proposal_id from batch_proposal)
  ),
  5::numeric,
  'PCS aliases preserve the extracted total quantity'
);
select is(
  (
    select count(*)::integer
    from public.item_variants
    where sku in ('PG-BULK-2W10', 'PG-BULK-2W25')
  ),
  2,
  'both catalog variants are created'
);
select is(
  (
    select status::text
    from public.catalog_batch_creation_requests
    where id = (select batch_id from catalog_batch)
  ),
  'completed',
  'the batch is completed atomically'
);

create temporary table atomic_batch_application as
select public.confirm_catalog_batch_and_apply_inventory(
  (select batch_id from catalog_batch),
  '11000000-0000-0000-0000-000000000001'
) as result;

select isnt(
  (
    select result ->> 'transaction_id'
    from atomic_batch_application
  ),
  null::text,
  'the combined confirmation returns the applied transaction ID'
);
select is(
  (
    select status::text
    from public.transaction_proposals
    where id = (select proposal_id from batch_proposal)
  ),
  'applied',
  'the same confirmation applies the original stock proposal'
);
select is(
  (
    select count(*)
    from public.transaction_lines
    where transaction_id = (
      select (result ->> 'transaction_id')::uuid
      from atomic_batch_application
    )
  ),
  2::bigint,
  'the applied transaction contains both newly created products'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type, status, payload
)
values
(
  '50000000-0000-0000-0000-000000000722',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'catalog-batch-details-outbox',
  'callback_query',
  'processing',
  '{}'::jsonb
),
(
  '50000000-0000-0000-0000-000000000723',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'catalog-batch-confirmation-outbox',
  'callback_query',
  'processing',
  '{}'::jsonb
);

select isnt(
  public.enqueue_processing_outcome(
    '10000000-0000-0000-0000-000000000001',
    '50000000-0000-0000-0000-000000000722',
    'catalog_batch_details_required',
    (select batch_id from catalog_batch),
    100000001,
    '{}'::jsonb
  ),
  null::uuid,
  'bulk detail prompts pass through the real outbox constraint'
);
select isnt(
  public.enqueue_processing_outcome(
    '10000000-0000-0000-0000-000000000001',
    '50000000-0000-0000-0000-000000000723',
    'catalog_batch_confirmation',
    (select batch_id from catalog_batch),
    100000001,
    '{}'::jsonb
  ),
  null::uuid,
  'bulk confirmations pass through the real outbox constraint'
);

select * from finish();
rollback;
