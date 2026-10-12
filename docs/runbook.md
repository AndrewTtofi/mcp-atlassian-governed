# Runbook

How to run and operate the server. It grows with each phase; today the server has one
tool and depends on an identity provider for sign-in.

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
| `ALLOW_ANONYMOUS` | `false` | `true` runs with no identity provider: anyone who can reach the port can call the server |

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
