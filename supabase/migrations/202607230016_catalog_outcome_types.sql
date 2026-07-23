alter type public.processing_outcome_type
  add value if not exists 'catalog_item_details_required';

alter type public.processing_outcome_type
  add value if not exists 'catalog_item_confirmation';
