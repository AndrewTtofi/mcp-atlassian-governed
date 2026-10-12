# 0002 — The server as an OAuth resource server, with a pre-registered client

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 2

## Context
The MCP authorization specification (revision 2026-07-28) makes an MCP server over HTTP
an OAuth 2.1 resource server. It must:

- publish protected resource metadata (RFC 9728) that names its authorization server;
- answer a request without a valid token with 401 and a `WWW-Authenticate` header that
  points at that metadata, and one without the needed scope with 403 `insufficient_scope`;
- accept only tokens issued for it, and never accept or pass on any other token.

Clients find everything from there: the metadata, the authorization server's own
metadata, then a sign-in in the browser with PKCE.

The specification also describes how a client gets a client id (client id metadata
documents preferred, dynamic registration deprecated, pre-registration allowed), and
expects the authorization server to bind a token to the resource the client names
(RFC 8707).

## Options considered
1. **Run an authorization server inside this server** (the SDK can) — everything the
   specification prefers, in one process. But then this server owns passwords, consent,
   sessions and token signing: the part of the system that should be the company's
   identity provider.
2. **A hosted identity provider** — realistic, but an account and a tunnel, and nothing
   that runs in CI.
3. **authentik in the compose stack** — open source, configured as code, runs in CI. It
   was measured against the specification's expectations by the end-to-end test:

   | Expectation | authentik 2026.8 |
   |-------------|------------------|
   | Discovery document (OIDC Discovery) | Yes |
   | PKCE with S256 | Yes |
   | Signed JWT access tokens, published keys | Yes (RS256, once a signing key is set) |
   | Custom scopes in the token | Yes |
   | Client id metadata documents | No |
   | Dynamic client registration | No |
   | `iss` in authorization responses (RFC 9207) | No |
   | Binding the token to the `resource` parameter (RFC 8707) | No: the parameter is ignored and `aud` is the client id |

## Decision
Option 3, with two consequences accepted:

- **The client is pre-registered.** One public client, `mcp-atlassian`, with PKCE and
  loopback redirect URIs. The specification allows this. A client has to be told the
  client id; it can't register itself.
- **Audience binding comes from one application per resource server.** authentik sets
  `aud` to the client id, so the application *is* the audience: a token is for this
  server exactly when it was issued through this application. The server requires
  `aud: mcp-atlassian`. The end-to-end test proves the property that matters: a valid
  token from the same provider, for another application, with the same scope, is refused.

What the server checks on every request (`app/tokens.py`):

- the signature, against the provider's published keys, **asymmetric algorithms only**;
- issuer, audience, expiry, and that there is a subject;
- the scope `atlassian:read`, or the answer is 403 `insufficient_scope`.

Further choices:

- **Keys are cached by key id.** An unknown key id triggers at most one refetch a minute,
  so invented key ids can't be used to hammer the provider. If the provider is down,
  tokens signed with keys already seen keep working.
- **A refusal doesn't say why.** The reason (expired, wrong audience, bad signature) goes
  to the server's log. The caller gets 401.
- **No identity provider, no start.** Without `OIDC_ISSUER` and `OIDC_AUDIENCE` the server
  refuses to start unless `ALLOW_ANONYMOUS=true`, so an open server is a choice someone
  made, not a missing variable.
- **The token stops here.** It identifies the caller to this server and is never sent on.
- **The SDK does the HTTP side** (metadata route, challenges, the scope check). This
  project supplies the token verifier.

## Consequences
- Any identity provider that issues signed JWT access tokens can replace authentik by
  changing two variables. One that supports RFC 8707 and client id metadata documents
  would remove both compromises above.
- Clients that only know how to register themselves can't connect; the client id has to
  be configured in them.
- The 401 challenge names the metadata but not the scope (the SDK doesn't add it); the
  scope is in the metadata's `scopes_supported` and in the 403.
- An identity-provider outage is a 401 for tokens signed with a key not yet seen, where a
  503 would be more accurate. The SDK's verifier interface can only say yes or no.
- Verified so far with tokens obtained by a script (a user's app password). The browser
  sign-in with PKCE is configured but has not been driven by a real MCP client yet.
