# Runbook

How to run and operate the server. It grows with each phase; today the server has one
tool and no dependencies.

## Run it

```bash
make up        # build and start; listens on 127.0.0.1:8080
make logs
make down
```

Without Docker: `make install`, then `python -m app`.

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

## When something is wrong

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
