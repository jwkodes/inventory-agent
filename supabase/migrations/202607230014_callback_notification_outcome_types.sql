alter type public.processing_outcome_type
  add value if not exists 'transaction_applied';

alter type public.processing_outcome_type
  add value if not exists 'callback_notice';
