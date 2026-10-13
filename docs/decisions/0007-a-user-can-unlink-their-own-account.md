# 0007 — A user can unlink their own Atlassian account

- **Status:** accepted
- **Date:** 2026-10-13
- **Phase:** after the first live run

## Context
Decision record 0003 assumed a grant has two states: it works, or Atlassian refuses it
and the user is offered a new link. The first run against a real site showed a third. The
user linked while the site had only Confluence. Jira was added afterwards, and every Jira
call answered "not found", then "forbidden": Atlassian fixes a grant to the products on
the site, and the scopes asked for, at the moment the user consents. The grant was still
valid and still refreshed, so the server never offered a link. The way out was to delete
the row with `sqlite3`.

The same thing happens by design when an operator turns on a write tool: users who linked
before hold a grant without the write scope.

There was also no way for a user to say "stop holding my tokens" short of asking an
operator.

## Options considered
1. **Leave it to the operator** (delete the row) — what the runbook said. Doesn't scale
   past a demo, and puts a person between a user and their own consent.
2. **Detect it**: on a 403 or 404 from Atlassian, drop the grant and offer a link.
   Those are also the ordinary answers for an issue the user may not see, so one
   mistyped key would unlink them.
3. **Compare scopes**: link again when the stored grant lacks a configured scope. Covers
   the write-scope case, not the product case: the scopes were all there, the product
   was not.
4. **A tool the user calls**: `atlassian_unlink`.

## Decision
Option 4. `atlassian_unlink` takes no arguments and deletes the caller's own grant. The
next Jira or Confluence call returns a link, as for any unlinked user.

- **Only the caller's own.** The subject comes from the verified token; there is no
  argument that could name someone else.
- **No confirmation.** A confirmation guards what can't be undone at someone else's
  expense (decision record 0004). This removes access from the server, and linking again
  restores it.
- **Not a question for the gateway.** The policy decides who may use Jira and Confluence
  through this server. It should not be able to stop someone withdrawing their grant,
  least of all a user it has just refused. The call is still rate limited and recorded
  (`tool:atlassian_unlink`, kind `account`).
- **Under the refresh lock.** A refresh stores a new token pair when it finishes. A delete
  that ran in the middle would be undone by it, and the user would still be linked. The
  delete waits for the per-user lock a refresh holds.
- **It needs only the read scope,** like every tool here.

## Consequences
- A user can recover from a stale grant, and operators have something to tell them.
- The server's copy is deleted; the grant at Atlassian is not revoked, because Atlassian documents
  no endpoint for an app to revoke an OAuth 2.0 (3LO) grant (checked 2026-10-13). The discarded refresh token
  can't be recovered from here, and expires unused. To end it at the source the user
  removes the app under Connected apps; the tool's answer says so.
- A model can call it unasked. The cost is one more sign-in and consent, so it is
  not confirmed, but it is on the record.
- Option 3 would still be worth adding for the write-scope case, so users aren't left to
  work out why a write fails. Not done.
- Tested against the stand-in only. It has not been run against a real site.
