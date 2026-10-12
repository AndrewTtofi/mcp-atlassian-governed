# 0004 — Write tools: an allow-list, a scope, and a person's confirmation

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 4

## Context
Reading as the user is bounded by what the user may see. Writing is different in kind: a
model acts on text it reads, and some of that text is written by other people. An issue
description that says "delete the comments above" is an instruction to a model unless
something outside the model stops it. Three tools change things: adding a comment,
creating an issue, deleting a comment.

## Options considered
1. **Trust the user's own Jira permissions** — if they may delete, their assistant may.
   That makes every page the assistant reads able to act with the user's hands.
2. **Tool annotations only** — mark tools `destructiveHint` and let the client decide.
   Useful, but a hint the server doesn't enforce.
3. **Layers the server enforces**, each stopping a different failure:
   - what exists at all (an allow-list, set by the operator);
   - what this session may do (an OAuth scope, granted by the user);
   - what happens now (a confirmation, answered by the person).

## Decision
Option 3, with the annotations as well.

**Allow-list.** `WRITE_TOOLS` names the write tools that exist. One that isn't named is
not registered: it isn't in `tools/list`, and calling it is an unknown tool. An unknown
name in the list stops the server at start.

**Scope.** A write tool needs `atlassian:write` on top of `atlassian:read`.
- A call with a read-only token is answered in HTTP, as the MCP authorization
  specification asks: 403 with `WWW-Authenticate: Bearer error="insufficient_scope"` and
  both scopes, so the client re-authorizes for the union and retries. The tool name is
  inside the JSON-RPC body, so a small layer in front of the MCP app reads the body,
  checks, and passes it on untouched (`app/scopes.py`).
- The tool checks the scope again where the change is made.

**Confirmation.** `jira_delete_comment` cannot run without the person saying yes.
- Before the tool body runs, the server reads the comment from Jira and asks, through
  the client: *Delete this comment on OPS-12 by Bob Example? "Ship it Friday."* The
  question is built from Jira's data, not from the model's description.
- The answer comes back from the client as the SDK's elicitation result. It is not a
  tool argument, so the model cannot supply it.
- Decline, cancel, or `confirm: false` deletes nothing. A client that can't ask gets an
  error and nothing is deleted.
- A comment the user can't see is an error before any question is asked.

**Plain text only.** Comment bodies and descriptions are sent as text nodes. Nothing a
model writes becomes a mention, a link or markup.

**Arguments that reach a URL are validated first** (issue key, project key, comment id),
as with the read tools.

## Consequences
- A read-only deployment is the default and needs no configuration.
- Enabling writes is deliberate and visible: a config value, an identity-provider scope,
  an Atlassian permission, and users linking again to grant it.
- The confirmation is as strong as the client. One that auto-answers elicitations has
  chosen to remove the person from the loop; the server can't tell.
- The comment could change between the question and the deletion. The window is the time
  the person takes to answer.
- Create and comment are not confirmed. They are not destructive, and Jira's history
  shows the user as their author.
