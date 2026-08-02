alter table public.catalog_item_edit_requests
  drop constraint catalog_item_edit_requests_organization_id_source_event_id_fkey,
  add constraint catalog_item_edit_requests_organization_id_source_event_id_fkey
    foreign key (organization_id, source_event_id)
    references public.source_events (organization_id, id)
    on delete set null (source_event_id),
  drop constraint catalog_item_edit_requests_organization_id_item_variant_id_fkey,
  add constraint catalog_item_edit_requests_organization_id_item_variant_id_fkey
    foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id)
    on delete cascade;

comment on constraint catalog_item_edit_requests_organization_id_source_event_id_fkey
  on public.catalog_item_edit_requests is
  'Retains the organization-scoped audit record when its transient source event is deleted.';
comment on constraint catalog_item_edit_requests_organization_id_item_variant_id_fkey
  on public.catalog_item_edit_requests is
  'Deletes catalog-edit audit rows only when the owning catalog variant is explicitly deleted.';
