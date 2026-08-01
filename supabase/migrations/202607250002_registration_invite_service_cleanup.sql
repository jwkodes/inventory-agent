-- Keep new registration tables consistent with the existing server-only table grants.
-- The application does not expose invite deletion; this permits controlled maintenance
-- and component-test cleanup through the service-role boundary.
grant delete on public.organization_registration_invites to service_role;
