# ADR-001: Invite-Only Public Multi-User Access for Thesis

- **Status:** Accepted (2026-09-12)
- **Supersedes:** the single-operator first-release assumption in `.github/architecture/fca-thesis.md` for named, invited users only.

## Context

Thesis was designed for one authenticated local operator. Supporting "multiple
human users, organizations, or public multi-tenancy" was an explicit first-release
non-goal, even though the data model already carries explicit workspace
ownership so authorization boundaries would not need collapsing later.

The product now needs a publicly reachable Thesis web app for a small set of
named people, with accounts created only through one-time invitation links, and
with the repository owner provisioned as the default account. That requires
public hosting, durable accounts/invites/sessions, email and OAuth
authentication, and per-request workspace authorization. None of these exist
today: the UI is not deployed, `thesis serve` is a scaffold, auth is a single
shared bearer token, and the session/authorization primitives are unwired and
in-memory.

## Decision

- **Hosting.** Serve the SvelteKit SPA from **Azure Static Web Apps** (Standard
  tier) with a **linked backend** that proxies `/api/*` to the Thesis Container
  App. The browser sees one origin; the Container App keeps internal ingress.
  Custom domain and managed TLS reuse the existing domain-hosting runbook.
- **Identity.** Use **Microsoft Entra External ID (CIAM)** for email/password
  and social (Google, GitHub) sign-in via OIDC authorization-code + PKCE. Tenant
  and app registration are a one-time tenant-admin runbook, not Bicep.
- **Accounts, invites, sessions are app-owned.** Thesis persists them in a
  durable store (`thesis.identity`: Azure Table in Azure, JSON/SQLite locally).
  Invite tokens are single-use, expiring, bound to an email where set, and
  **hashed at rest**; the raw token exists only in the emailed link.
- **Sessions.** The API validates the IdP token, then issues a server-side
  session referenced by an `HttpOnly; Secure; SameSite=Lax` cookie with a CSRF
  token. The in-memory session store is replaced with the durable store so
  scale-to-zero and multiple replicas work. Revocation is server-side.
- **Authorization.** `WorkspaceAuthorizer` is enforced on every request and
  `account_id` is mapped to workspace `owner_id`; anonymous and cross-workspace
  access fail closed.
- **Default owner.** Seed an owner account from `THESIS_OWNER_EMAIL`; the first
  sign-in matching that email claims `role=owner`. Existing `local-operator`
  workspaces are migrated to the owner account.
- **Scope.** Invite-only named users. Open signup and organization/multi-tenant
  collaboration remain non-goals.

## Consequences

- The `fca-thesis.md` multi-user non-goal is lifted for invite-only named users;
  the architecture and Phase 6 roadmap are updated to reference this ADR.
- New `thesis.identity` package and durable session store; `thesis serve` must
  become a real ASGI app; a UI build/publish workflow is added.
- New tenant-admin bootstrap (Entra External ID app + Azure Communication
  Services email domain) and Key Vault-only secret handling for the mailer.
- Security obligations: TLS everywhere; HttpOnly/Secure/SameSite cookies +
  CSRF; strict redirect-URI/origin allowlists; single-use, expiring, hashed
  invite tokens with rate limiting; login/invite/consent audit events; secrets
  never in Git or workspace repositories.

## Alternatives Considered

- **Co-serving the SPA from the Container App** (same-origin without Static Web
  Apps). Viable and simpler, but keeps static delivery off a CDN-tier host;
  rejected in favor of Static Web Apps.
- **Entra ID B2B guest invitations or a third-party auth SaaS (Auth0/Clerk).**
  B2B does not model true one-time app links; SaaS adds a vendor and moves
  identity outside Azure. Rejected.
- **Stateless signed-cookie sessions.** Rejected because revocation and
  single-use invite redemption need authoritative server-side state.

## Update 2026-09-12: API reachability

Azure Static Web Apps do not support network-isolated (internal-ingress) linked
backends. The chosen linked-backend topology therefore requires the Thesis API
Container App to use **external ingress** (a public FQDN). The API remains
default-deny: every `/v1` route is authenticated and workspace-authorized, and
the Static Web App remains the only public site hostname (the Container App
binds no custom domain). The composition enables external ingress automatically
whenever the Static Web App is enabled (`deployThesisStaticWebApp`). Rejected
alternative: Azure Front Door Premium with a Private Link origin to keep the API
private, at higher cost and complexity.
