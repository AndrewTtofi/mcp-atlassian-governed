# Runbook

How to run and operate the server. It grows with each phase. The server depends on an
identity provider for sign-in and on Atlassian Cloud for its tools, and keeps one SQLite
file.

## Run it

```bash
make up        # build and start; listens on 127.0.0.1:8080
make logs
make down
```

Without Docker: `make install`, then `ALLOW_ANONYMOUS=true python -m app`.

With sign-in: `make idp` adds authentik on `http://localhost:9000` (admin user `akadmin`,
password `AUTHENTIK_BOOTSTRAP_PASSWORD` from `.env`). `make idp-down` stops both.

## Check it

| Check | Command | Healthy |
|-------|---------|---------|
| The process answers | `curl -s localhost:8080/healthz` | `{"status":"ok","version":"…"}` |
| The container | `docker compose ps` | `Up … (healthy)` |
| MCP answers | Connect an MCP client to `http://localhost:8080/mcp` and call `server_status` | A result with the version |

## Configuration

Everything is an environment variable; `.env.example` lists them all.

| Variable | Default | Meaning |
|----------|---------|---------|
| `HOST`, `PORT` | `127.0.0.1`, `8080` | Where the server listens. The compose file sets `HOST=0.0.0.0` inside the container and publishes the port on `127.0.0.1` only |
| `ALLOWED_HOSTS` | `localhost:8080,127.0.0.1:8080` | The names clients may use to reach the server |
| `LOG_LEVEL` | `INFO` | |
| `OIDC_ISSUER` | empty | Exactly the `iss` of the tokens to accept |
| `OIDC_AUDIENCE` | empty | Must be in the tokens' `aud`. With authentik: the application's client id |
| `OIDC_JWKS_URL` | empty | Where the provider's keys are. Default: from the issuer's discovery document. Set it when the server reaches the provider at a different address than users do |
| `RESOURCE_URL` | `http://localhost:8080/mcp` | This server's address as clients know it. It is published in the metadata and must match what clients connect to |
| `READ_SCOPE` | `atlassian:read` | The scope every call needs |
| `WRITE_SCOPE` | `atlassian:write` | The scope a tool that changes something also needs |
| `WRITE_TOOLS` | empty | The write tools that exist, comma-separated: `jira_add_comment`, `jira_create_issue`, `jira_delete_comment`. Empty: read-only |
| `ALLOW_ANONYMOUS` | `false` | `true` runs with no identity provider: anyone who can reach the port can call the server |
| `OIDC_AUTHORIZE_URL`, `OIDC_TOKEN_URL` | empty | Where a browser signs in, and where the server redeems the code. Default: from discovery. Set both when users and the server reach the provider at different addresses |
| `PUBLIC_URL` | `http://localhost:8080` | The server's address in a user's browser. The Atlassian app's callback must be `PUBLIC_URL/atlassian/callback` |
| `ATLASSIAN_CLIENT_ID`, `ATLASSIAN_CLIENT_SECRET` | empty | The OAuth 2.0 (3LO) app. Both empty: no Atlassian tools. **The secret is a secret** |
| `ATLASSIAN_SCOPES` | read scopes + `offline_access` | What users are asked to grant. Must be a subset of the app's permissions |
| `TOKEN_ENCRYPTION_KEY` | empty | **Secret.** Encrypts stored Atlassian tokens. `python -m app.store` prints a new one |
| `DATABASE_PATH` | `data/mcp.sqlite3` | The SQLite file. In the container it is on the `mcp-data` volume |

## When something is wrong

### The server exits at start: "set OIDC_ISSUER and OIDC_AUDIENCE…"
No identity provider is configured. Set both, or `ALLOW_ANONYMOUS=true` for local use.

### A client gets 401 with a token
The response never says why; the server's log does (`access token refused: …`):

| Log says | Cause | Fix |
|----------|-------|-----|
| `InvalidAudienceError` | The token was issued for another application | The client must sign in through this server's application (client id = `OIDC_AUDIENCE`) |
| `InvalidIssuerError` | `iss` differs from `OIDC_ISSUER`, often by host name or a trailing slash | Set `OIDC_ISSUER` to exactly the `iss` in a token |
| `ExpiredSignatureError` | The token is past its lifetime | The client refreshes or signs in again |
| `signed with a key the identity provider doesn't list` | The provider's keys can't be fetched, or it rotated them within the last minute | Check `OIDC_JWKS_URL` is reachable from the server; rotated keys are picked up within a minute |
| `algorithm or key id not acceptable` | A symmetric or unsigned token. With authentik: the provider has no signing key set | Set a signing key on the provider |

### A client gets 403 `insufficient_scope`
The token is valid but lacks `atlassian:read`. The client must request that scope, and
the identity provider's application must offer it.

### A user is told to link their account again and again
Their grant keeps dying. Likely causes: the app's permissions in the developer console no
longer cover `ATLASSIAN_SCOPES`; the user revoked the app in their Atlassian account; or
`TOKEN_ENCRYPTION_KEY` changed without keeping the old key (see below).

### "This link was made for a different user"
The browser was signed in at the identity provider as someone other than the user who
asked for the link. Sign out there, or use the right browser profile, and ask the
assistant for a new link. If nobody asked for the link, it was sent by someone else:
ignore it. It linked nothing.

### "This link is not valid"
Links last ten minutes, work once, and a newer link voids an older one. Call the tool
again for a fresh one.

### Rotate the token encryption key
1. `python -m app.store` prints a new key.
2. Set `TOKEN_ENCRYPTION_KEY=<new>,<old>` and restart. New writes use the new key; old
   rows are still readable and are re-encrypted as each user's token is next refreshed.
3. After an hour of normal use (or once you accept that idle users will link again),
   set `TOKEN_ENCRYPTION_KEY=<new>` and restart.

Losing the key is not a breach: the stored tokens can't be read by anyone. Every user
links again.

### Revoke one user's Atlassian access
`sqlite3 data/mcp.sqlite3 "DELETE FROM grants WHERE subject = '<sub>'"`. The user can
also remove the app under their Atlassian account's connected apps, which kills the grant
at the source.

### Back up
The `mcp-data` volume (one file). A backup is only useful together with the key, and
should be protected like it is.

### Turn a write tool on or off
Set `WRITE_TOOLS` and restart. With any write tool on, the Atlassian app needs the
`write:jira-work` permission and `ATLASSIAN_SCOPES` must include it; users who linked
before that have to link again to grant it. A name that isn't a tool stops the server at
start, so a typo can't silently leave a tool off.

### A client gets 403 on a write tool
Its token has `atlassian:read` but not `atlassian:write`. The response's
`WWW-Authenticate` header names both scopes; the client asks the user for them and
retries. The identity provider's application must offer the write scope.

### "Client did not declare the form elicitation capability"
The client can't show the user a confirmation, so the destructive tool refuses. Nothing
was deleted. Use a client that supports elicitation, or delete in Jira.

### Read the audit record
Every tool call, account link and scope refusal is a row in `audit` and a JSON line from
the `mcp.audit` logger. Neither holds arguments or results.

```bash
# the last 20 things that happened
sqlite3 -header -column data/mcp.sqlite3 \
  "SELECT datetime(at,'unixepoch') AS at, subject, event, kind, outcome, detail FROM audit ORDER BY id DESC LIMIT 20"

# everything that changed Jira in the last 7 days, by user
sqlite3 -header -column data/mcp.sqlite3 \
  "SELECT subject, event, count(*) FROM audit WHERE kind != 'read' AND outcome = 'ok'
   AND at > unixepoch() - 7*86400 GROUP BY subject, event"

# what was refused
sqlite3 -header -column data/mcp.sqlite3 \
  "SELECT datetime(at,'unixepoch') AS at, subject, event, outcome, detail FROM audit
   WHERE outcome IN ('refused','rate_limited','not_confirmed') ORDER BY id DESC LIMIT 50"
```

In the container the file is `/srv/data/mcp.sqlite3` on the `mcp-data` volume.
`subject` is the identity provider's `sub`; look the person up there.

### A user says "Too many calls in the last minute"
They, or an agent acting for them, passed `CALLS_PER_MINUTE` or `WRITES_PER_MINUTE`. The
audit record shows `rate_limited` rows and what was being called. Limits are per process
and reset on restart.

### The identity provider is down
Tokens signed with keys the server has already seen keep working until they expire.
Nobody can sign in or refresh. Key fetches are retried at most once a minute.

### A client gets `421 Misdirected Request`
The `Host` header it sent is not in `ALLOWED_HOSTS`. This is the protection against DNS
rebinding doing its job. If the server is reached through a new name or port (a proxy, a
different published port), add `name:port` to `ALLOWED_HOSTS` and restart.

### A client gets `400 Missing session ID`
It sent a request before `initialize`. MCP clients do this handshake themselves; a
hand-written `curl` has to send `initialize` first and then pass the `mcp-session-id`
header it gets back.

### The container is `unhealthy`
`docker compose logs mcp`. The healthcheck calls `/healthz` inside the container on port
8080; a different `PORT` in `.env` breaks it, because the compose file and the image
both expect 8080.

## Dependencies

Pinned with hashes in `requirements.txt`. To change one, edit `requirements.in`, run
`make lock`, and commit both files. `make audit` checks the lockfile for known
vulnerabilities; CI runs it on every pull request.
