begin;

create extension if not exists pgtap with schema extensions;

select plan(17);

select has_function(
  'public',
  'mark_proposal_line_as_new_item',
  array['uuid', 'uuid'],
  'one unmatched line can be selected for new-item creation'
);
select has_function(
  'public',
  'mark_all_unmatched_proposal_lines_as_new',
  array['uuid', 'uuid'],
  'all remaining unmatched lines can be selected together'
);
select has_function(
  'public',
  'ignore_inventory_proposal_line',
  array['uuid', 'uuid'],
  'one mistaken proposal line can be ignored'
);

insert into public.transaction_proposals (
  id,
  organization_id,
  location_id,
  created_by,
  intent,
  idempotency_key,
  raw_command
)
values (
  '40000000-0000-0000-0000-000000000850',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'multi-line-ignore-test',
  '{"intent":"RECEIVE_STOCK"}'::jsonb
);

insert into public.proposal_lines (
  id,
  organization_id,
  proposal_id,
  line_number,
  source_text,
  extracted_description,
  requested_quantity,
  requested_unit,
  item_variant_id,
  base_quantity_delta,
  base_unit,
  match_method,
  match_score,
  match_evidence
)
values
(
  '41000000-0000-0000-0000-000000000851',
  '10000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000850',
  1,
  'Full Cream Milk 1L',
  'MILK',
  2,
  'each',
  '21000000-0000-0000-0000-000000000002',
  2,
  'each',
  'exact_identifier',
  1,
  '{"decision":"matched"}'::jsonb
),
(
  '41000000-0000-0000-0000-000000000852',
  '10000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000850',
  2,
  '2W-25 DC24V BRASS N/C G1 inch',
  'SOLENOID VALVE',
  4,
  'PCS',
  null,
  null,
  null,
  null,
  null,
  '{"decision":"not_found","candidates":[]}'::jsonb
);

select is(
  (
    public.get_proposal_confirmation_view(
      '40000000-0000-0000-0000-000000000850'
    ) #>> '{lines,1,description}'
  ),
  '2W-25 DC24V BRASS N/C G1 inch',
  'proposal review preserves the distinguishing source description'
);
select is(
  public.mark_proposal_line_as_new_item(
    '41000000-0000-0000-0000-000000000852',
    '11000000-0000-0000-0000-000000000001'
  ),
  '40000000-0000-0000-0000-000000000850'::uuid,
  'selecting add-new returns the proposal for a fresh review'
);
select is(
  (
    select match_evidence ->> 'user_resolution'
    from public.proposal_lines
    where id = '41000000-0000-0000-0000-000000000852'
  ),
  'add_new',
  'the add-new decision is durable'
);
select is(
  public.ignore_inventory_proposal_line(
    '41000000-0000-0000-0000-000000000852',
    '11000000-0000-0000-0000-000000000001'
  ),
  '40000000-0000-0000-0000-000000000850'::uuid,
  'a selected new line can instead be ignored before confirmation'
);
select is(
  (
    select match_evidence ->> 'user_resolution'
    from public.proposal_lines
    where id = '41000000-0000-0000-0000-000000000852'
  ),
  'ignored',
  'the ignored decision is retained for audit'
);
select lives_ok(
  $$
    select public.apply_inventory_proposal(
      '40000000-0000-0000-0000-000000000850',
      '11000000-0000-0000-0000-000000000001'
    )
  $$,
  'a proposal applies while safely skipping an ignored line'
);
select is(
  (
    select count(*)
    from public.transaction_lines as line
    join public.inventory_transactions as transaction
      on transaction.id = line.transaction_id
    where transaction.proposal_id = '40000000-0000-0000-0000-000000000850'
  ),
  1::bigint,
  'the applied transaction contains only the active line'
);
select is(
  (
    select count(*)
    from public.proposal_lines
    where proposal_id = '40000000-0000-0000-0000-000000000850'
  ),
  2::bigint,
  'the ignored proposal line remains available for audit'
);

insert into public.transaction_proposals (
  id,
  organization_id,
  location_id,
  created_by,
  intent,
  idempotency_key,
  raw_command
)
values (
  '40000000-0000-0000-0000-000000000860',
  '10000000-0000-0000-0000-000000000001',
  '12000000-0000-0000-0000-000000000001',
  '11000000-0000-0000-0000-000000000001',
  'receive_stock',
  'multi-line-message-test',
  '{"intent":"RECEIVE_STOCK"}'::jsonb
);

insert into public.proposal_lines (
  id,
  organization_id,
  proposal_id,
  line_number,
  source_text,
  extracted_description,
  requested_quantity,
  requested_unit,
  match_evidence
)
values
(
  '41000000-0000-0000-0000-000000000861',
  '10000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000860',
  1,
  'Valve DC24V normally closed',
  'SOLENOID VALVE',
  1,
  'PCS',
  '{"decision":"not_found","candidates":[]}'::jsonb
),
(
  '41000000-0000-0000-0000-000000000862',
  '10000000-0000-0000-0000-000000000001',
  '40000000-0000-0000-0000-000000000860',
  2,
  'Valve 220V normally open',
  'SOLENOID VALVE',
  2,
  'PCS',
  '{"decision":"not_found","candidates":[]}'::jsonb
);

select is(
  public.mark_all_unmatched_proposal_lines_as_new(
    '40000000-0000-0000-0000-000000000860',
    '11000000-0000-0000-0000-000000000001'
  ),
  '40000000-0000-0000-0000-000000000860'::uuid,
  'a non-invoice multi-line proposal enters the same bulk workflow'
);
select is(
  (
    select count(*)
    from public.proposal_lines
    where proposal_id = '40000000-0000-0000-0000-000000000860'
      and match_evidence ->> 'user_resolution' = 'add_new'
  ),
  2::bigint,
  'all unmatched lines receive explicit add-new decisions'
);

create temporary table message_batch as
select public.begin_catalog_batch_creation(
  '40000000-0000-0000-0000-000000000860',
  '11000000-0000-0000-0000-000000000001',
  100000860
) as batch_id;

select is(
  (
    select count(*)
    from public.catalog_item_creation_requests
    where batch_id = (select batch_id from message_batch)
  ),
  2::bigint,
  'both selected new items enter one catalog batch'
);
select is(
  (
    public.get_catalog_batch_creation_view(
      (select batch_id from message_batch)
    ) #>> '{items,0,suggested_name}'
  ),
  'Valve DC24V normally closed',
  'the first batch item keeps its distinguishing description'
);
select is(
  (
    public.get_catalog_batch_creation_view(
      (select batch_id from message_batch)
    ) #>> '{items,1,suggested_name}'
  ),
  'Valve 220V normally open',
  'the second batch item keeps its distinguishing description'
);

select throws_ok(
  $$
    select public.ignore_inventory_proposal_line(
      '41000000-0000-0000-0000-000000000851',
      '11000000-0000-0000-0000-000000000001'
    )
  $$,
  '22023',
  'Proposal line cannot be ignored',
  'a matched line cannot be silently excluded'
);

select * from finish();
rollback;
