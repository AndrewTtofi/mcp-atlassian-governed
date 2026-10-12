# 0005 — An audit record per call, and rate limits per user

- **Status:** accepted
- **Date:** 2026-10-13
- **Phase:** 5

## Context
Atlassian's own audit log shows what a user's account did. It cannot show that an
assistant did it, which calls were refused before they reached Atlassian, or that someone
tried to link an account from the wrong browser. And nothing so far stops an agent in a
loop from making thousands of calls with a user's access.

## Options considered
1. **Log lines only** — simple, but they live wherever logs go and are rotated away.
2. **A row per call in the database the server already has, plus the log line.**
3. **Send records to the gateway's decision log** — one place for everything, and the
   right end state, but it makes every tool call depend on another service being up.

For limits: none; a global limit; or a limit per user.

## Decision
**Option 2 for the record.** One record per `tools/call`, written as a JSON log line
(logger `mcp.audit`) and a row in the `audit` table:

| Field | Example |
|-------|---------|
| `subject` | the identity provider's `sub`, or `anonymous` |
| `event` | `tool:jira_delete_comment`, `link`, … |
| `kind` | `read`, `write`, `destructive`, `account` |
| `outcome` | `ok`, `error`, `refused`, `asked`, `not_confirmed`, `rate_limited` |
| `detail` | a fixed word (`insufficient_scope`, `opened_by_another_user`) or an exception class |
| `ms` | how long it took |

- **Never content.** No arguments, no results, no tokens. The record shows that a comment
  was deleted and by whom, not what it said: that is in Jira's own history.
- **A tool name the server doesn't know is recorded as `unknown`.** The name is client
  input; writing it down would let a caller write anything into the log.
- **It sits in the MCP layer as middleware,** so it sees every call whatever happens next:
  an unknown tool, a refused argument, a question to the user, a failure at Atlassian.
- **A deletion is two records:** `asked` when the person is shown the question, then `ok`
  or `not_confirmed`.
- **Outside tool calls** it also records account linking (linked, declined, opened by
  another user) and write calls refused for a missing scope.
- **A failed write to the table doesn't fail the call.** The log line was already
  emitted, and is the record of last resort.
- Rows older than `AUDIT_RETENTION_DAYS` (365) are deleted when the server starts.

**Limits per user:** `CALLS_PER_MINUTE` (60) for all tool calls and `WRITES_PER_MINUTE`
(10) for those that change something, as a sliding window in memory. A refused call gets
a tool error, is recorded, and never reaches Atlassian.

## Consequences
- "Who used the assistant to change Jira last week, and what was refused?" is one query.
- The limits are per process and reset on restart. They exist to stop a runaway agent
  spending a user's Atlassian quota or flooding a project, not as a security boundary.
- The table shares a file with the grants, so it shares their backup and their single
  replica.
- Sending these records to the gateway's decision log is left for when this server is
  governed by the gateway's policy (phase 6).
