alter table public.processing_outbox
  drop constraint processing_outbox_aggregate_check,
  add constraint processing_outbox_aggregate_check check (
    (
      outcome_type in (
        'proposal_ready',
        'transaction_applied',
        'catalog_item_details_required',
        'catalog_item_confirmation',
        'catalog_batch_details_required',
        'catalog_batch_confirmation',
        'reversal_reason_required',
        'reversal_confirmation'
      )
      and aggregate_id is not null
    )
    or (
      outcome_type not in (
        'proposal_ready',
        'transaction_applied',
        'catalog_item_details_required',
        'catalog_item_confirmation',
        'catalog_batch_details_required',
        'catalog_batch_confirmation',
        'reversal_reason_required',
        'reversal_confirmation'
      )
      and aggregate_id is null
    )
  );

comment on constraint processing_outbox_aggregate_check
  on public.processing_outbox is
  'Requires aggregate IDs for every durable workflow view, including catalog batches.';
