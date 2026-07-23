begin;

create extension if not exists pgtap with schema extensions;

select plan(18);

select has_function(
  'public',
  'claim_processing_outbox',
  array['uuid'],
  'atomic outbox claim function exists'
);
select has_function(
  'public',
  'finish_processing_outbox',
  array['uuid', 'boolean', 'text', 'integer'],
  'outbox completion function exists'
);
select has_function(
  'public',
  'get_proposal_confirmation_view',
  array['uuid'],
  'proposal confirmation view function exists'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type, status, processed_at
)
values (
  '50000000-0000-0000-0000-000000000005',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'outbox-delivery-test-update',
  'message',
  'processed',
  now()
);

insert into public.processing_outbox (
  id, organization_id, source_event_id, outcome_type, chat_id, payload
)
values (
  '60000000-0000-0000-0000-000000000005',
  '10000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000005',
  'clarification_required',
  100000001,
  '{"message":"Which item?"}'::jsonb
);

create temporary table claimed_outbox as
select * from public.claim_processing_outbox(
  '60000000-0000-0000-0000-000000000005'
);

select is((select count(*) from claimed_outbox), 1::bigint, 'due outcome is claimed');
select is((select attempt_number from claimed_outbox), 1, 'claim increments attempt count');
select is(
  (
    select status::text from public.processing_outbox
    where id = '60000000-0000-0000-0000-000000000005'
  ),
  'sending',
  'claim records the in-flight state'
);
select is(
  (
    select count(*) from public.claim_processing_outbox(
      '60000000-0000-0000-0000-000000000005'
    )
  ),
  0::bigint,
  'an active delivery cannot be claimed twice'
);
select is(
  public.finish_processing_outbox(
    '60000000-0000-0000-0000-000000000005', false, 'temporary failure', 0
  ),
  'pending',
  'a delivery failure schedules a retry'
);

create temporary table retried_outbox as
select * from public.claim_processing_outbox(
  '60000000-0000-0000-0000-000000000005'
);

select is((select attempt_number from retried_outbox), 2, 'retry increments attempt count');
select is(
  public.finish_processing_outbox(
    '60000000-0000-0000-0000-000000000005', true, null, 30
  ),
  'sent',
  'successful delivery is recorded'
);
select isnt(
  (
    select sent_at from public.processing_outbox
    where id = '60000000-0000-0000-0000-000000000005'
  ),
  null::timestamptz,
  'successful delivery records its timestamp'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type
)
values (
  '50000000-0000-0000-0000-000000000006',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'outbox-undone-source-test',
  'message'
);
insert into public.processing_outbox (
  id, organization_id, source_event_id, outcome_type, chat_id, payload
)
values (
  '60000000-0000-0000-0000-000000000006',
  '10000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000006',
  'clarification_required',
  100000001,
  '{"message":"not ready"}'::jsonb
);
select is(
  (
    select count(*) from public.claim_processing_outbox(
      '60000000-0000-0000-0000-000000000006'
    )
  ),
  0::bigint,
  'outcome waits until its source event is processed'
);

insert into public.source_events (
  id, organization_id, provider, external_event_id, event_type
)
values (
  '50000000-0000-0000-0000-000000000007',
  '10000000-0000-0000-0000-000000000001',
  'telegram',
  'outbox-proposal-view-test',
  'message'
);

select public.create_inventory_proposal(
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '50000000-0000-0000-0000-000000000007',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'outbox-confirmation-view-resolved',
  '{}'::jsonb,
  null, null, null, null,
  '[{
    "line_number": 1,
    "source_text": "three milk",
    "extracted_description": "Full Cream Milk 1L",
    "requested_quantity": 3,
    "item_variant_id": "21000000-0000-0000-0000-000000000002",
    "match_method": "exact_identifier",
    "match_score": 1
  }]'::jsonb
);

select is(
  public.get_proposal_confirmation_view(
    (
      select id from public.transaction_proposals
      where idempotency_key = 'outbox-confirmation-view-resolved'
    )
  ) ->> 'intent',
  'receive_stock',
  'confirmation view includes proposal intent'
);
select is(
  public.get_proposal_confirmation_view(
    (
      select id from public.transaction_proposals
      where idempotency_key = 'outbox-confirmation-view-resolved'
    )
  ) #>> '{lines,0,matched_label}',
  'Full Cream Milk 1L · MILK-FULLCREAM-1L',
  'resolved line includes its matched label'
);
select is(
  public.get_proposal_confirmation_view(
    (
      select id from public.transaction_proposals
      where idempotency_key = 'outbox-confirmation-view-resolved'
    )
  ) #>> '{lines,0,quantity}',
  '3.00000000',
  'confirmation quantity preserves database precision'
);

update public.processing_outbox
set status = 'sending', attempts = 5
where id = '60000000-0000-0000-0000-000000000006';
select is(
  public.finish_processing_outbox(
    '60000000-0000-0000-0000-000000000006', false, 'permanent failure', 30
  ),
  'failed',
  'fifth failure dead-letters the outcome'
);
select is(
  (
    select status::text from public.processing_outbox
    where id = '60000000-0000-0000-0000-000000000006'
  ),
  'failed',
  'dead-lettered outcome retains failed status'
);
select is(
  (
    select count(*) from public.claim_processing_outbox(
      '60000000-0000-0000-0000-000000000006'
    )
  ),
  0::bigint,
  'dead-lettered outcome is not claimed again'
);

select * from finish();
rollback;
