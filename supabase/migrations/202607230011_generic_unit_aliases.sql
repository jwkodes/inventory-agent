create or replace function public.add_default_variant_unit_conversions()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.item_unit_conversions (
    organization_id,
    item_variant_id,
    from_unit,
    factor_to_base
  )
  select
    new.organization_id,
    new.id,
    alias.from_unit,
    1
  from (
    values ('unit'), ('units'), ('item'), ('items')
  ) as alias(from_unit)
  on conflict (organization_id, item_variant_id, from_unit) do nothing;

  return new;
end;
$$;

insert into public.item_unit_conversions (
  organization_id,
  item_variant_id,
  from_unit,
  factor_to_base
)
select
  variant.organization_id,
  variant.id,
  alias.from_unit,
  1
from public.item_variants as variant
cross join (
  values ('unit'), ('units'), ('item'), ('items')
) as alias(from_unit)
on conflict (organization_id, item_variant_id, from_unit) do nothing;

create trigger item_variants_add_default_unit_conversions
after insert on public.item_variants
for each row execute function public.add_default_variant_unit_conversions();

revoke all on function public.add_default_variant_unit_conversions() from public;

comment on function public.add_default_variant_unit_conversions() is
  'Adds factor-one aliases for explicitly generic references to one matched SKU unit.';
