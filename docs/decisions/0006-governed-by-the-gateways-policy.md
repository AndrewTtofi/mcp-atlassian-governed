# 0006 — Governed by the gateway's policy

- **Status:** accepted
- **Date:** 2026-10-13
- **Phase:** 6

## Context
This server answers two questions by itself: who is calling (the identity provider's
token) and what they can reach (their own Atlassian grant). A third is not its to answer:
*should* this person be using this tool, given their role and the class of data? That is
the organisation's policy. If every tool server keeps its own copy, the copies drift, and
"who can use the Jira tools?" has as many answers as there are servers.

The governed AI gateway this project sits beside already holds that policy (group → role
→ tier, data classes, which role may use which tool) and a decision log.

## Options considered
1. **A policy file here** — no dependency, and a second source of truth.
2. **Send the user's token to the gateway** — forbidden: a resource server must not pass
   on the token it was given, and the gateway must not accept a token issued for
   someone else.
3. **Token exchange** (RFC 8693): trade the user's token for one the gateway accepts.
   The right mechanism where the identity provider supports it; the bundled one doesn't.
4. **Ask the gateway for a decision, with this server's own credential.**

## Decision
Option 4. With `GATEWAY_URL` and `GATEWAY_KEY` set, every Atlassian tool call is first
put to the gateway:

```
POST {GATEWAY_URL}/v1/authorize      Authorization: Bearer {GATEWAY_KEY}
{"subject": "<the token's sub>", "connector": "atlassian", "tool": "jira_add_comment", "data_class": "internal"}
→ {"allowed": false, "code": "role_not_allowed", …}
```

- **Anything but a clear yes is a no.** An outage, a non-200 answer, an answer that
  can't be read, or `allowed` that isn't `true`: the call is refused and nothing runs.
- **The user's token is never sent.** The gateway gets the subject and resolves the user
  from its own directory, so someone deactivated there is refused here on their next call.
- **The question is asked in the same middleware as the audit record,** before the tool
  body and before Atlassian is touched. A refusal is recorded here with the gateway's
  code, and by the gateway under the user's name.
- **The person is told plainly, without the gateway's reasoning.** The reason stays in
  the gateway's log.
- **No caching of answers.** One request per tool call; a cached yes would outlive a
  deprovisioning.
- **`server_status` is not asked about.** It is about this server, not Atlassian.
- **Without the two variables the server runs ungoverned,** on its own controls: scopes,
  the allow-list, the confirmation, the user's own Atlassian permissions.

## Consequences
- One policy file and one decision log cover these tools and the gateway's own.
- The gateway is on the path of every tool call: its outage stops the tools. That is the
  fail-closed choice, and it costs a round trip per call.
- The gateway takes this server's word for who the user is. The gateway limits who may
  ask (a named group of services) and logs who asked.
- Both sides must agree on the user identifier: the `sub` in this server's tokens has to
  be what the gateway matches in its directory.
- The two services are tested against each other's documented contract, each with a
  stand-in for the other. They have not been run together yet.
