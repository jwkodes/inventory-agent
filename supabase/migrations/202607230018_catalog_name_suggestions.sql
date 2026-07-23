create or replace function public.catalog_item_name_suggestion(
  p_proposal_line_id uuid
)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select coalesce(
    nullif(btrim(line.extracted_description), ''),
    nullif(btrim(raw_line.value #>> '{item_reference,value}'), ''),
    nullif(btrim(line.source_text), '')
  )
  from public.proposal_lines as line
  join public.transaction_proposals as proposal
    on proposal.id = line.proposal_id
  left join lateral (
    select entry.value
    from jsonb_array_elements(coalesce(proposal.raw_command -> 'lines', '[]'::jsonb))
      with ordinality as entry(value, position)
    where entry.position = line.line_number
    limit 1
  ) as raw_line on true
  where line.id = p_proposal_line_id;
$$;

create or replace function public.set_catalog_item_suggested_name()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  new.suggested_name := coalesce(
    public.catalog_item_name_suggestion(new.proposal_line_id),
    new.suggested_name
  );
  return new;
end;
$$;

drop trigger if exists catalog_item_creation_suggested_name
  on public.catalog_item_creation_requests;
create trigger catalog_item_creation_suggested_name
before insert or update of proposal_line_id, suggested_name
on public.catalog_item_creation_requests
for each row execute function public.set_catalog_item_suggested_name();

update public.catalog_item_creation_requests as request
set suggested_name = public.catalog_item_name_suggestion(request.proposal_line_id),
    updated_at = now()
where request.suggested_name is distinct from
  public.catalog_item_name_suggestion(request.proposal_line_id);

revoke all on function public.catalog_item_name_suggestion(uuid) from public;
revoke all on function public.catalog_item_name_suggestion(uuid) from anon, authenticated;

comment on function public.catalog_item_name_suggestion(uuid) is
  'Derives a quantity-free catalog name from a proposal description or item reference.';
