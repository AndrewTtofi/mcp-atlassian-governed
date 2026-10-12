# 0003 — Per-user Atlassian grants: linking, storage and refresh

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 3

## Context
The token a caller presents says who they are to this server and must not be passed on
(decision record 0001). To call Jira and Confluence *as that user*, the server needs a
second grant from the same person: Atlassian's OAuth 2.0 authorization code flow ("3LO"),
which yields an access token (about an hour) and a rotating refresh token.

That raises three questions: how a user in an MCP client, which is not a browser, gives
that consent; where the tokens live; and how they are kept fresh.

## Options considered
**Who is at the browser.** The consent happens at a URL the user opens.
1. **Trust the URL** — whoever opens it links their Atlassian account to the identity the
   URL was made for. An attacker sends their own URL to a victim; the victim consents;
   the victim's Jira is now readable by the attacker.
2. **A code the user copies between the assistant and the page** — friction, and still
   works on a victim who is told to type it.
3. **Make the browser sign in first** — before Atlassian is involved, the browser goes
   through the identity provider (authorization code with PKCE), and the ID token's
   subject must equal the user the URL was made for.

**Where the tokens live.**
1. **In memory** — nothing to protect, and every restart makes every user link again.
2. **Postgres or a secrets manager** — right for several replicas; too much for one
   small server.
3. **One SQLite file, tokens encrypted before they are written.**

## Decision
**Linking: option 3.** A tool called by an unlinked user returns a one-time URL, valid ten
minutes. The browser is sent to the identity provider; only if it comes back as the same
user is it sent on to Atlassian; the callback stores the grant for that user. Each step
has its own unguessable state, each state works once, and the link is deleted when it is
used, refused or declined. A new link voids the previous one.

**Storage: option 3.** `grants` holds one row per user: site, account name, and the two
tokens encrypted with Fernet (AES with an authentication tag) under a key that exists
only in the environment. Several keys can be configured: the first encrypts, all
decrypt, so a key is rotated by prepending a new one. A row that no configured key can
read is deleted, and that user links again.

**Refresh.** An access token within a minute of expiry is refreshed before use.
- One refresh per user at a time. Atlassian's refresh tokens work once; two concurrent
  refreshes would leave one caller with a token that has just been disabled.
- The new pair is stored before it is used, so a crash can't lose the only valid
  refresh token.
- If Atlassian refuses the refresh (revoked, expired after 90 days unused, password
  changed), the grant is deleted and the next call returns a new link.

**The tools** (`jira_search`, `jira_get_issue`, `confluence_search`,
`confluence_get_page`) call `api.atlassian.com/ex/{product}/{cloud id}/…` with the
user's own token.
- Anything that goes into a URL path is validated first (an issue key, a numeric page
  id). Queries (JQL, CQL) go as parameters and are bounded in length.
- Results are cut down to a few fields; long text is truncated at 6,000 characters.
- "Not found" and "no access" are the same answer, as they are from Atlassian.

## Consequences
- The first use costs two redirects (sign in, then consent). After that, nothing until
  the grant dies.
- The database file is not a credential without the key. The key is: it belongs in a
  secret store, and losing it means everyone links again, not a breach.
- One replica. SQLite and the in-process refresh lock both assume it. Several replicas
  would need Postgres and a lock all of them see.
- One Atlassian site per user: the first the grant can reach.
- The link URL passes through the model's context. That is acceptable because the URL is
  useless to anyone who can't sign in as the user it was made for.
- **Atlassian has not been called for real yet.** Everything here is tested against a
  stand-in that follows Atlassian's documented requests and responses. The first run
  against a real site may find differences, most likely in scopes and response shapes.
