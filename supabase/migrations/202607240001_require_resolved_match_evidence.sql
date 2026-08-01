alter table public.proposal_lines
  add constraint proposal_lines_resolved_match_method_check
  check (item_variant_id is null or match_method is not null)
  not valid;

comment on constraint proposal_lines_resolved_match_method_check
  on public.proposal_lines is
  'Resolved proposal lines created after this constraint must retain their grounding method.';
