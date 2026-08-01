alter table public.catalog_batch_creation_requests
  drop constraint
    catalog_batch_creation_reques_organization_id_details_sour_fkey;

alter table public.catalog_batch_creation_requests
  add foreign key (organization_id, details_source_event_id)
    references public.source_events (organization_id, id)
    on delete set null (details_source_event_id);
