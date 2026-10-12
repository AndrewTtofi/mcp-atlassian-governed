# Plan

A governed MCP server for Jira and Confluence: the agent acts as the signed-in user.
One phase per branch. A phase is done when its tests, lint and CI pass and the docs say
what changed. Decisions are recorded in `docs/decisions/`.

### Phase 1 — Scaffold (`main`, first commits)
- [x] MCP server over Streamable HTTP with one tool that has nothing behind it.
- [x] Tests: the tool over MCP in-process, and the HTTP surface.
- [x] Dockerfile and compose stack with a healthcheck; hashed lockfiles.
- [x] CI: lint, types, tests, dependency audit, the container end to end.

### Phase 2 — The server as an OAuth resource server (`phase-2-resource-server`)
- [ ] Protected resource metadata (`/.well-known/oauth-protected-resource`).
- [ ] 401 with `WWW-Authenticate` pointing at it; 403 `insufficient_scope` with the scope needed.
- [ ] Access tokens verified: signature, issuer, audience (this server only), expiry, scope.
- [ ] An identity provider in the compose stack, and an end-to-end test against it.

### Phase 3 — Atlassian account linking and read tools (`phase-3-atlassian-read`)
Needs an Atlassian Cloud site and an OAuth 2.0 (3LO) app.
- [ ] Each user links their own Atlassian account; the grant is stored encrypted.
- [ ] Refresh-token rotation.
- [ ] `jira_search`, `jira_get_issue`, `confluence_search`, `confluence_get_page`.

### Phase 4 — Write and destructive tools (`phase-4-writes`)
- [ ] `jira_add_comment`, `jira_create_issue`: a write scope, and an allow-list in config.
- [ ] `jira_delete_comment`: also a confirmation the user answers in their client.

### Phase 5 — Audit and hardening (`phase-5-audit`)
- [ ] One record per call, allowed or refused. Never content.
- [ ] Rate limits per user; results bounded and marked as untrusted.

### Phase 6 — Governed by the gateway, and presentation (`phase-6-gateway`)
- [ ] The gateway's policy decides who may use which tool.
- [ ] README, demo, architecture, runbook brought up to date; repository made public.
