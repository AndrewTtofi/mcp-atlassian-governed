# 0001 — A separate MCP server, where the agent acts as the user

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 1

## Context
An assistant that can read and change Jira and Confluence needs access to them. The
common shortcut is one service account or admin API token shared by everyone: the
assistant then sees every project and space, whoever is asking, and the other system's
audit log shows one account doing everything.

The Model Context Protocol gives assistants a standard way to call tools, and its
authorization specification (revision 2026-07-28) defines how a server over HTTP is
protected. Two of its rules decide the shape of this project:

- an MCP server is an **OAuth resource server**: it accepts only access tokens issued for
  it, and checks that they were;
- an MCP server **must not accept or pass on any other token**. It cannot forward the
  caller's token to the system behind it.

## Options considered
1. **A shared Atlassian API token in the server's environment** — a day's work. Everyone
   gets the token owner's reach; nothing ties an action to the person who asked.
2. **Pass the caller's token through to Atlassian** — looks like "acting as the user", but
   the specification forbids it, and for good reason: a token for one audience would be
   accepted by another, and the server could be used to reach whatever that token reaches.
3. **Two grants per user** — the caller proves who they are with a token issued for this
   server. Separately, each user links their own Atlassian account once (OAuth 2.0
   authorization code), and the server keeps that grant and uses it only for that user.
4. **Put the tools in the gateway** (the governed AI gateway this project sits beside) —
   one less service, but the gateway's connectors act as an app, not as a person, and MCP
   clients could not use the tools directly.

## Decision
Option 3, as its own repository and service. Built in phases:

1. the server, its transport and its container (this phase);
2. the server as an OAuth resource server;
3. Atlassian account linking and read tools;
4. write tools on an allow-list, and a destructive tool that needs the user's confirmation;
5. an audit record per call, and hardening;
6. governed by the gateway's policy.

Choices made now:

- **Python 3.12 and the official MCP SDK (2.x)**, over Streamable HTTP. The SDK carries
  the protocol and the bearer-token middleware; this project adds what is specific to it.
- **Stateless process, one small store later.** Phase 1 has no storage at all.
- **Host names are an allow-list** (`ALLOWED_HOSTS`). An MCP server on a developer's
  machine is reachable from their browser; without the check, a web page could drive it
  through a DNS name it controls.
- **The server tells the model how to treat results.** Its instructions say that text
  from Jira and Confluence is written by other people and is never an instruction.

## Consequences
- Each person sees and changes only what their own Atlassian account allows, and
  Atlassian's audit log names them.
- There is a per-user grant to store and refresh, which is a secret store to protect
  (phase 3).
- Two sign-ins the first time: the identity provider, then Atlassian.
- A second service to run next to the gateway.
