create or replace function public.get_proposal_confirmation_view(p_proposal_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  v_result jsonb;
begin
  select jsonb_build_object(
    'proposal_id', proposal.id,
    'intent', proposal.intent,
    'lines', coalesce(
      jsonb_agg(
        jsonb_build_object(
          'proposal_line_id', line.id,
          'description', coalesce(nullif(trim(line.source_text), ''), line.extracted_description),
          'quantity', line.requested_quantity::text,
          'unit', line.requested_unit,
          'matched_label', case
            when variant.id is null then null
            else coalesce(variant.name, item.name) || ' · ' || variant.sku
          end,
          'match_decision', line.match_evidence ->> 'decision',
          'clarification_question', line.match_evidence ->> 'clarification_question',
          'show_candidates', coalesce(
            (line.match_evidence ->> 'show_candidates')::boolean,
            false
          ),
          'user_resolution', line.match_evidence ->> 'user_resolution',
          'new_item_preview', case
            when jsonb_typeof(line.match_evidence -> 'new_item') = 'object'
              and nullif(trim(line.match_evidence #>> '{new_item,name}'), '') is not null
              and nullif(trim(line.match_evidence #>> '{new_item,sku}'), '') is not null
              and nullif(trim(line.match_evidence #>> '{new_item,base_unit}'), '') is not null
              and line.match_evidence #>> '{new_item,tracking_mode}' = 'simple'
            then line.match_evidence -> 'new_item'
            else null
          end,
          'candidate_choices', case
            when variant.id is not null
              or line.match_evidence ->> 'user_resolution' = 'ignored'
            then '[]'::jsonb
            else coalesce(
              (
                select jsonb_agg(
                  jsonb_build_object(
                    'item_variant_id', candidate.value ->> 'item_variant_id',
                    'label',
                      coalesce(
                        candidate.value ->> 'variant_name',
                        candidate.value ->> 'item_name',
                        candidate.value ->> 'sku',
                        'Unknown item'
                      ) || case
                        when candidate.value ->> 'sku' is null then ''
                        else ' · ' || (candidate.value ->> 'sku')
                      end
                  )
                  order by candidate.ordinality
                )
                from jsonb_array_elements(
                  coalesce(line.match_evidence -> 'candidates', '[]'::jsonb)
                ) with ordinality as candidate(value, ordinality)
                where candidate.value ->> 'item_variant_id' is not null
              ),
              '[]'::jsonb
            )
          end
        )
        order by line.line_number
      ),
      '[]'::jsonb
    )
  ) into v_result
  from public.transaction_proposals as proposal
  join public.proposal_lines as line
    on line.organization_id = proposal.organization_id
   and line.proposal_id = proposal.id
  left join public.item_variants as variant
    on variant.organization_id = line.organization_id
   and variant.id = line.item_variant_id
  left join public.items as item
    on item.organization_id = variant.organization_id
   and item.id = variant.item_id
  where proposal.id = p_proposal_id
  group by proposal.id, proposal.intent;

  if v_result is null then
    raise exception using
      errcode = 'P0002',
      message = 'Proposal confirmation view was not found';
  end if;
  return v_result;
end;
$$;

comment on function public.get_proposal_confirmation_view(uuid) is
  'Renders proposal review data, including complete agent-proposed catalog previews.';
