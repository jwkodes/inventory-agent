create extension if not exists pgcrypto with schema extensions;
create extension if not exists pg_trgm with schema extensions;

create type public.organization_role as enum ('worker', 'manager', 'admin');
create type public.tracking_mode as enum ('simple', 'lot', 'serial');
create type public.custom_field_entity as enum (
  'item',
  'variant',
  'lot',
  'serial',
  'transaction'
);
create type public.custom_field_data_type as enum (
  'text',
  'number',
  'date',
  'boolean',
  'enum'
);
create type public.identifier_type as enum (
  'sku',
  'barcode',
  'manufacturer_part_number',
  'supplier_part_number'
);
create type public.source_event_status as enum ('received', 'processing', 'processed', 'failed');
create type public.proposal_intent as enum ('receive_stock', 'issue_stock', 'adjust_stock');
create type public.proposal_status as enum (
  'pending_confirmation',
  'applied',
  'rejected',
  'expired'
);
create type public.match_method as enum (
  'exact_identifier',
  'confirmed_alias',
  'text_search',
  'semantic_rerank',
  'human_selected'
);
create type public.inventory_transaction_type as enum (
  'receive',
  'issue',
  'adjustment',
  'reversal'
);
create type public.inventory_transaction_status as enum ('applied');

create table public.organizations (
  id uuid primary key default extensions.gen_random_uuid(),
  name text not null,
  slug text not null unique,
  inventory_profile text not null default 'general',
  settings jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (id, slug)
);

create table public.organization_users (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  auth_user_id uuid references auth.users (id) on delete set null,
  telegram_user_id bigint,
  display_name text not null,
  role public.organization_role not null default 'worker',
  active boolean not null default true,
  created_at timestamptz not null default now(),
  unique (organization_id, id),
  unique (organization_id, auth_user_id),
  unique (organization_id, telegram_user_id),
  check (auth_user_id is not null or telegram_user_id is not null)
);

create table public.locations (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  code text not null,
  name text not null,
  active boolean not null default true,
  attributes jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (organization_id, id),
  unique (organization_id, code)
);

create table public.custom_field_definitions (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  entity_type public.custom_field_entity not null,
  key text not null,
  label text not null,
  data_type public.custom_field_data_type not null,
  required_on_receive boolean not null default false,
  required_on_issue boolean not null default false,
  searchable boolean not null default false,
  enum_options jsonb,
  validation_rules jsonb not null default '{}'::jsonb,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  unique (organization_id, entity_type, key),
  check (
    (data_type = 'enum' and jsonb_typeof(enum_options) = 'array')
    or (data_type <> 'enum' and enum_options is null)
  )
);

create table public.items (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  name text not null,
  base_unit text not null,
  tracking_mode public.tracking_mode not null default 'simple',
  attributes jsonb not null default '{}'::jsonb,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, id),
  check (length(trim(base_unit)) > 0)
);

create table public.item_variants (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  item_id uuid not null,
  sku text not null,
  name text,
  attributes jsonb not null default '{}'::jsonb,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (organization_id, item_id)
    references public.items (organization_id, id) on delete cascade,
  unique (organization_id, id),
  unique (organization_id, sku)
);

create table public.item_identifiers (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  item_variant_id uuid not null,
  identifier_type public.identifier_type not null,
  value text not null,
  normalized_value text not null,
  supplier_scope text not null default '',
  created_at timestamptz not null default now(),
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id) on delete cascade,
  unique (organization_id, identifier_type, normalized_value, supplier_scope),
  check (length(trim(normalized_value)) > 0)
);

create table public.item_unit_conversions (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  item_variant_id uuid not null,
  from_unit text not null,
  factor_to_base numeric(24, 8) not null,
  created_at timestamptz not null default now(),
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id) on delete cascade,
  unique (organization_id, item_variant_id, from_unit),
  check (factor_to_base > 0)
);

create table public.inventory_lots (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  item_variant_id uuid not null,
  lot_number text not null,
  manufactured_on date,
  expires_on date,
  attributes jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id) on delete cascade,
  unique (organization_id, id, item_variant_id),
  unique (organization_id, item_variant_id, lot_number),
  check (expires_on is null or manufactured_on is null or expires_on >= manufactured_on)
);

create table public.inventory_serials (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  item_variant_id uuid not null,
  serial_number text not null,
  attributes jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id) on delete cascade,
  unique (organization_id, id, item_variant_id),
  unique (organization_id, item_variant_id, serial_number)
);

create table public.inventory_balances (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  location_id uuid not null,
  item_variant_id uuid not null,
  lot_id uuid,
  serial_id uuid,
  quantity numeric(24, 8) not null default 0,
  version bigint not null default 0,
  updated_at timestamptz not null default now(),
  foreign key (organization_id, location_id)
    references public.locations (organization_id, id) on delete cascade,
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id) on delete cascade,
  foreign key (organization_id, lot_id, item_variant_id)
    references public.inventory_lots (organization_id, id, item_variant_id),
  foreign key (organization_id, serial_id, item_variant_id)
    references public.inventory_serials (organization_id, id, item_variant_id),
  unique nulls not distinct (organization_id, location_id, item_variant_id, lot_id, serial_id),
  check (not (lot_id is not null and serial_id is not null)),
  check (quantity >= 0)
);

create table public.item_aliases (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  item_variant_id uuid not null,
  supplier_scope text not null default '',
  source_text text not null,
  normalized_source_text text not null,
  confirmed_by uuid not null,
  confirmed_at timestamptz not null default now(),
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id) on delete cascade,
  foreign key (organization_id, confirmed_by)
    references public.organization_users (organization_id, id),
  unique (organization_id, supplier_scope, normalized_source_text)
);

create table public.source_events (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null references public.organizations (id) on delete cascade,
  provider text not null,
  external_event_id text not null,
  event_type text not null,
  status public.source_event_status not null default 'received',
  payload jsonb not null default '{}'::jsonb,
  error_message text,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  unique (provider, external_event_id),
  unique (organization_id, id)
);

create table public.source_artifacts (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  source_event_id uuid,
  storage_bucket text not null,
  storage_path text not null,
  media_type text not null,
  sha256 text,
  telegram_file_id text,
  transcript text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  foreign key (organization_id, source_event_id)
    references public.source_events (organization_id, id) on delete set null,
  unique (storage_bucket, storage_path)
);

create table public.transaction_proposals (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  location_id uuid not null,
  source_event_id uuid,
  created_by uuid not null,
  confirmed_by uuid,
  intent public.proposal_intent not null,
  status public.proposal_status not null default 'pending_confirmation',
  idempotency_key text not null,
  raw_command jsonb not null default '{}'::jsonb,
  model_name text,
  model_response_id text,
  prompt_version text,
  schema_version text not null default '1.0',
  notes text,
  created_at timestamptz not null default now(),
  confirmed_at timestamptz,
  applied_transaction_id uuid,
  foreign key (organization_id, location_id)
    references public.locations (organization_id, id),
  foreign key (organization_id, source_event_id)
    references public.source_events (organization_id, id),
  foreign key (organization_id, created_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, confirmed_by)
    references public.organization_users (organization_id, id),
  unique (organization_id, id),
  unique (organization_id, idempotency_key)
);

create table public.proposal_lines (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  proposal_id uuid not null,
  line_number integer not null,
  source_text text not null,
  extracted_description text,
  requested_quantity numeric(24, 8) not null,
  requested_unit text,
  item_variant_id uuid,
  lot_id uuid,
  serial_id uuid,
  base_quantity_delta numeric(24, 8),
  base_unit text,
  match_method public.match_method,
  match_score numeric(8, 7),
  match_evidence jsonb not null default '{}'::jsonb,
  attributes jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  foreign key (organization_id, proposal_id)
    references public.transaction_proposals (organization_id, id) on delete cascade,
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id),
  foreign key (organization_id, lot_id, item_variant_id)
    references public.inventory_lots (organization_id, id, item_variant_id),
  foreign key (organization_id, serial_id, item_variant_id)
    references public.inventory_serials (organization_id, id, item_variant_id),
  unique (organization_id, proposal_id, line_number),
  check (line_number > 0),
  check (requested_quantity > 0),
  check (match_score is null or (match_score >= 0 and match_score <= 1)),
  check (not (lot_id is not null and serial_id is not null))
);

create table public.inventory_transactions (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  location_id uuid not null,
  proposal_id uuid,
  reversal_of_transaction_id uuid,
  transaction_type public.inventory_transaction_type not null,
  status public.inventory_transaction_status not null default 'applied',
  created_by uuid not null,
  confirmed_by uuid not null,
  reason text,
  notes text,
  applied_at timestamptz not null default now(),
  foreign key (organization_id, location_id)
    references public.locations (organization_id, id),
  foreign key (organization_id, proposal_id)
    references public.transaction_proposals (organization_id, id),
  foreign key (organization_id, created_by)
    references public.organization_users (organization_id, id),
  foreign key (organization_id, confirmed_by)
    references public.organization_users (organization_id, id),
  foreign key (reversal_of_transaction_id)
    references public.inventory_transactions (id),
  unique (organization_id, id),
  unique (proposal_id),
  unique (reversal_of_transaction_id),
  check (
    (transaction_type = 'reversal' and reversal_of_transaction_id is not null)
    or (transaction_type <> 'reversal' and reversal_of_transaction_id is null)
  )
);

alter table public.transaction_proposals
  add constraint transaction_proposals_applied_transaction_fk
  foreign key (organization_id, applied_transaction_id)
  references public.inventory_transactions (organization_id, id);

create table public.transaction_lines (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  transaction_id uuid not null,
  line_number integer not null,
  source_proposal_line_id uuid,
  reversal_of_transaction_line_id uuid,
  item_variant_id uuid not null,
  lot_id uuid,
  serial_id uuid,
  quantity_delta numeric(24, 8) not null,
  base_unit text not null,
  quantity_before numeric(24, 8) not null,
  quantity_after numeric(24, 8) not null,
  source_text text,
  match_method public.match_method,
  match_score numeric(8, 7),
  match_evidence jsonb not null default '{}'::jsonb,
  attributes jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  foreign key (organization_id, transaction_id)
    references public.inventory_transactions (organization_id, id) on delete restrict,
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id),
  foreign key (organization_id, lot_id, item_variant_id)
    references public.inventory_lots (organization_id, id, item_variant_id),
  foreign key (organization_id, serial_id, item_variant_id)
    references public.inventory_serials (organization_id, id, item_variant_id),
  foreign key (source_proposal_line_id)
    references public.proposal_lines (id),
  foreign key (reversal_of_transaction_line_id)
    references public.transaction_lines (id),
  unique (organization_id, transaction_id, line_number),
  unique (source_proposal_line_id),
  unique (reversal_of_transaction_line_id),
  check (line_number > 0),
  check (quantity_delta <> 0),
  check (quantity_after = quantity_before + quantity_delta),
  check (quantity_after >= 0),
  check (not (lot_id is not null and serial_id is not null))
);

create table public.stock_movements (
  id uuid primary key default extensions.gen_random_uuid(),
  organization_id uuid not null,
  transaction_id uuid not null,
  transaction_line_id uuid not null,
  location_id uuid not null,
  item_variant_id uuid not null,
  lot_id uuid,
  serial_id uuid,
  quantity_delta numeric(24, 8) not null,
  occurred_at timestamptz not null default now(),
  foreign key (organization_id, transaction_id)
    references public.inventory_transactions (organization_id, id) on delete restrict,
  foreign key (transaction_line_id)
    references public.transaction_lines (id) on delete restrict,
  foreign key (organization_id, location_id)
    references public.locations (organization_id, id),
  foreign key (organization_id, item_variant_id)
    references public.item_variants (organization_id, id),
  foreign key (organization_id, lot_id, item_variant_id)
    references public.inventory_lots (organization_id, id, item_variant_id),
  foreign key (organization_id, serial_id, item_variant_id)
    references public.inventory_serials (organization_id, id, item_variant_id),
  unique (transaction_line_id),
  check (quantity_delta <> 0),
  check (not (lot_id is not null and serial_id is not null))
);

create index item_variants_name_trgm_idx
  on public.item_variants using gin ((coalesce(name, '')) extensions.gin_trgm_ops);
create index items_name_trgm_idx
  on public.items using gin (name extensions.gin_trgm_ops);
create index item_aliases_source_trgm_idx
  on public.item_aliases using gin (normalized_source_text extensions.gin_trgm_ops);
create index inventory_lots_expiry_idx
  on public.inventory_lots (organization_id, expires_on)
  where expires_on is not null;
create index stock_movements_lookup_idx
  on public.stock_movements (organization_id, item_variant_id, occurred_at desc);
create index proposals_pending_idx
  on public.transaction_proposals (organization_id, created_at)
  where status = 'pending_confirmation';

insert into storage.buckets (id, name, public, file_size_limit)
values ('inventory-source-artifacts', 'inventory-source-artifacts', false, 52428800)
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit;

do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'organizations',
    'organization_users',
    'locations',
    'custom_field_definitions',
    'items',
    'item_variants',
    'item_identifiers',
    'item_unit_conversions',
    'inventory_lots',
    'inventory_serials',
    'inventory_balances',
    'item_aliases',
    'source_events',
    'source_artifacts',
    'transaction_proposals',
    'proposal_lines',
    'inventory_transactions',
    'transaction_lines',
    'stock_movements'
  ]
  loop
    execute format('alter table public.%I enable row level security', table_name);
  end loop;
end;
$$;

revoke all on all tables in schema public from anon, authenticated;
grant usage on schema public to service_role;
grant select, insert, update, delete on all tables in schema public to service_role;
