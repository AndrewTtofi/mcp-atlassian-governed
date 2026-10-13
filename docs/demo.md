# Demo

What can be shown without any account, and what needs an Atlassian site.

## 1. Sign-in, against a real identity provider (no accounts needed)

```bash
make idp-e2e
```

Starts the server and authentik in a throwaway stack, creates a user, gets a real token
for them and checks each answer (about two minutes):

```
  ok   no token: 401, and the challenge points at http://localhost:8080/.well-known/oauth-protected-resource/mcp
  ok   the metadata names this server and its authorization server
  ok   the authorization server is discoverable and supports PKCE (S256)
  ok   alice has a token for audience mcp-atlassian with scope atlassian:read
  ok   the tool answers and knows who is calling (dc9b44571620…)
  ok   a token without atlassian:read: 403 insufficient_scope
  ok   a valid token for another application, with the same scope: 401
  ok   a tampered token: 401
```

The line to point at is the second-to-last: a valid token from the same identity
provider, with the right scope, issued for a different application, is refused.

## 2. Everything else, in the tests

Atlassian is a stand-in in the test suite, built from Atlassian's documented requests and
responses. Each of these is one test you can run and read:

```bash
make install
pytest -q -k "links_their_account"            # sign in, consent, then read as yourself
pytest -q -k "opened_by_someone_else"         # an attacker's link opened by a victim links nothing
pytest -q -k "encrypted_in_the_database"      # the token is not in the file
pytest -q -k "concurrent_calls_refresh_once"  # ten calls, one refresh
pytest -q -k "unlinks_their_own_account"      # unlink, link again, and only ever your own
pytest -q -k "policy_refuses_can_still_unlink"
pytest -q -k "allow_list"                     # write tools that aren't enabled don't exist
pytest -q -k "403_that_names_the_scope"       # a read-only token calling a write tool
pytest -q -k "shows_the_person_the_real_comment"   # the confirmation before a deletion
pytest -q -k "anything_but_yes_deletes_nothing"
pytest -q -k "never_holds_what_was_said"      # the audit record has no content
pytest -q -k "anything_but_a_clear_yes_is_a_no"    # the gateway is down: nothing runs
```

## 3. With a real Atlassian site

Follow "Connect Atlassian" in the README, then:

```bash
make idp
```

Connect an MCP client to `http://localhost:8080/mcp` with client id `mcp-atlassian`, sign
in, and ask for an issue. The first call returns a link; open it, sign in, consent at
Atlassian, and call again.

This was run once, on 2026-10-13, with the MCP Inspector as the client and the gateway
deciding each call: sign-in, the link, a Confluence search and a Jira search. Two things
came out of it that the stand-in could not show:

- The identity provider named its token's issuer after the host the request arrived on.
  The server reached it by its internal name, so the sign-in step of linking was refused
  until the request carried the issuer's host.
- The site had only Confluence when the user linked. After Jira was added, Jira calls
  answered "not found": the grant was fixed at consent. That is why `atlassian_unlink`
  exists.

Not run live yet: the write tools, the confirmation, the two "get" tools, a refresh.
