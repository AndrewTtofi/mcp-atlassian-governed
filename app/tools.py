"""The Jira and Confluence tools. Each runs with the calling user's own Atlassian grant.

What comes back is trimmed to a few fields and bounded in size, and it is content other
people wrote: the server's instructions tell the model to treat it as information only.
"""

from __future__ import annotations

import html
import re
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.atlassian import Atlassian, AtlassianError, Grants, NotLinked
from app.linking import Linking
from app.store import Grant

ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,19}-[0-9]{1,9}")
PAGE_ID = re.compile(r"[0-9]{1,20}")
MAX_RESULTS = 25
TEXT_CHARS = 6000  # a description or page body is cut here
READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# Changes what this server holds, nothing at Atlassian. Doing it twice changes nothing more.
ACCOUNT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# What kind each tool is, for the audit record and the rate limit.
KINDS = {
    "server_status": "read",
    "jira_search": "read",
    "jira_get_issue": "read",
    "confluence_search": "read",
    "confluence_get_page": "read",
    "jira_add_comment": "write",
    "jira_create_issue": "write",
    "jira_delete_comment": "destructive",
    "atlassian_unlink": "account",
}
_TAGS = re.compile(r"<[^>]+>")


def adf_text(node: Any) -> str:
    """Plain text from Atlassian Document Format (the JSON Jira stores rich text in)."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    inner = "".join(adf_text(child) for child in node.get("content") or [])
    block = node.get("type") in ("paragraph", "heading", "listItem", "codeBlock", "blockquote")
    return inner + ("\n" if block else "")


def storage_text(markup: str) -> str:
    """Plain text from Confluence's storage format (XHTML)."""
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub(" ", markup))).strip()


def bounded(text: str) -> dict[str, Any]:
    return {"text": text[:TEXT_CHARS], "truncated": len(text) > TEXT_CHARS}


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
        raise ToolError(f"limit must be between 1 and {MAX_RESULTS}")
    return limit


def _name(person: Any) -> str | None:
    return person.get("displayName") if isinstance(person, dict) else None


def issue_summary(issue: dict[str, Any], site_url: str) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    status = fields.get("status")
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": status.get("name") if isinstance(status, dict) else None,
        "assignee": _name(fields.get("assignee")),
        "updated": fields.get("updated"),
        "url": f"{site_url}/browse/{issue.get('key')}",
    }


def register(
    server: MCPServer,
    grants: Grants,
    atlassian: Atlassian,
    linking: Linking,
    write_tools: frozenset[str] = frozenset(),
    write_scope: str = "atlassian:write",
    who: Any = get_access_token,
) -> None:
    """`who` returns the caller's access token; tests supply their own."""

    async def grant() -> Grant:
        """The caller's grant, or an error that tells them how to link their account."""
        caller = who()
        if caller is None or not caller.subject:
            raise ToolError("sign-in is required to use Jira and Confluence")
        try:
            return await grants.current(caller.subject)
        except NotLinked as exc:
            link = await grants.store.create_link(caller.subject)
            raise ToolError(
                f"{exc}. Open this link in your browser to connect it, then try again "
                f"(valid for 10 minutes): {linking.url_for(link)}"
            ) from None
        except AtlassianError as exc:
            raise ToolError(str(exc)) from None

    async def call(how: Any, *args: Any) -> Any:
        try:
            return await how(*args)
        except NotLinked:
            # Atlassian no longer accepts the token (revoked there): drop it and say so.
            caller = who()
            if caller and caller.subject:
                await grants.forget(caller.subject)
            raise ToolError(
                "your Atlassian link is no longer valid; call the tool again to get a new link"
            ) from None
        except AtlassianError as exc:
            raise ToolError(str(exc)) from None

    register_writes(server, write_tools, write_scope, grant, call, atlassian, who)

    @server.tool(
        title="Disconnect your Atlassian account",
        description="Remove your Atlassian link from this server: to stop using it, or to "
        "link again after your access at Atlassian changed (a product added to the site, "
        "new permissions). The next Jira or Confluence call returns a new link.",
        annotations=ACCOUNT,
    )
    async def atlassian_unlink() -> dict[str, Any]:
        # No confirmation: it only takes access away from this server, and linking again
        # undoes it. Only ever the caller's own grant; there is no argument to name another.
        caller = who()
        if caller is None or not caller.subject:
            raise ToolError("sign-in is required to use Jira and Confluence")
        was_linked = await grants.forget(caller.subject)
        return {
            "unlinked": was_linked,
            "atlassian": "not linked",
            # Atlassian documents no way for an app to revoke a 3LO grant: say where it is done.
            "note": "The tokens held here are deleted. To withdraw the app's access at "
            "Atlassian as well, remove it under Connected apps in your Atlassian account.",
        }

    @server.tool(
        title="Search Jira",
        description="Search Jira issues you can see, with JQL (for example: "
        "project = OPS AND status != Done ORDER BY updated DESC).",
        annotations=READ,
    )
    async def jira_search(jql: str, limit: int = 10) -> dict[str, Any]:
        if not jql.strip() or len(jql) > 1000:
            raise ToolError("jql must be between 1 and 1000 characters")
        g = await grant()
        params = {
            "jql": jql,
            "maxResults": _limit(limit),
            "fields": "summary,status,assignee,updated",
        }
        found = await call(atlassian.jira, g, "/rest/api/3/search/jql", params)
        issues = found.get("issues") if isinstance(found, dict) else None
        return {
            "issues": [issue_summary(i, g.site_url) for i in issues or [] if isinstance(i, dict)]
        }

    @server.tool(
        title="Get a Jira issue",
        description="One Jira issue by key (for example OPS-12): status, people and description.",
        annotations=READ,
    )
    async def jira_get_issue(key: str) -> dict[str, Any]:
        # The key goes into the URL path: only an issue key, nothing that reaches elsewhere.
        if not ISSUE_KEY.fullmatch(key):
            raise ToolError("key must look like PROJ-123")
        g = await grant()
        fields = "summary,status,assignee,reporter,priority,issuetype,created,updated,description"
        issue = await call(atlassian.jira, g, f"/rest/api/3/issue/{key}", {"fields": fields})
        if not isinstance(issue, dict):
            raise ToolError("Atlassian sent an unexpected response")
        f = issue.get("fields") or {}
        kind, priority = f.get("issuetype"), f.get("priority")
        return {
            **issue_summary(issue, g.site_url),
            "type": kind.get("name") if isinstance(kind, dict) else None,
            "priority": priority.get("name") if isinstance(priority, dict) else None,
            "reporter": _name(f.get("reporter")),
            "created": f.get("created"),
            "description": bounded(adf_text(f.get("description")).strip()),
        }

    @server.tool(
        title="Search Confluence",
        description="Search Confluence pages you can see, with CQL (for example: "
        'type = page AND text ~ "runbook").',
        annotations=READ,
    )
    async def confluence_search(cql: str, limit: int = 10) -> dict[str, Any]:
        if not cql.strip() or len(cql) > 1000:
            raise ToolError("cql must be between 1 and 1000 characters")
        g = await grant()
        found = await call(
            atlassian.confluence, g, "/wiki/rest/api/search", {"cql": cql, "limit": _limit(limit)}
        )
        results = found.get("results") if isinstance(found, dict) else None
        pages = []
        for r in results or []:
            content = r.get("content") if isinstance(r, dict) else None
            if not isinstance(content, dict):
                continue
            pages.append(
                {
                    "id": content.get("id"),
                    "title": content.get("title") or r.get("title"),
                    "type": content.get("type"),
                    "excerpt": storage_text(str(r.get("excerpt") or ""))[:300],
                    "url": f"{g.site_url}/wiki{r.get('url') or ''}",
                }
            )
        return {"pages": pages}

    @server.tool(
        title="Get a Confluence page",
        description="One Confluence page by its numeric id: title, space and text.",
        annotations=READ,
    )
    async def confluence_get_page(page_id: str) -> dict[str, Any]:
        if not PAGE_ID.fullmatch(page_id):
            raise ToolError("page_id must be a number")
        g = await grant()
        page = await call(
            atlassian.confluence,
            g,
            f"/wiki/rest/api/content/{page_id}",
            {"expand": "body.storage,version,space"},
        )
        if not isinstance(page, dict):
            raise ToolError("Atlassian sent an unexpected response")
        body = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
        space, version = page.get("space"), page.get("version")
        return {
            "id": page.get("id"),
            "title": page.get("title"),
            "space": space.get("key") if isinstance(space, dict) else None,
            "version": version.get("number") if isinstance(version, dict) else None,
            "url": f"{g.site_url}/wiki/pages/viewpage.action?pageId={page_id}",
            "body": bounded(storage_text(str(body))),
        }


# --- tools that change things (decision record 0004) -----------------------------------

WRITE_TOOLS = frozenset({"jira_add_comment", "jira_create_issue", "jira_delete_comment"})
DESTRUCTIVE_TOOLS = frozenset({"jira_delete_comment"})
PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,19}")
COMMENT_ID = re.compile(r"[0-9]{1,20}")
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def adf(text: str) -> dict[str, Any]:
    """Plain text as Atlassian Document Format: one paragraph per line. Plain text only,
    so nothing the model writes can become a mention, a link or markup."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []}
        for line in text.splitlines() or [""]
    ]
    return {"type": "doc", "version": 1, "content": paragraphs}


class Confirm(BaseModel):
    confirm: bool = Field(description="Delete it? This cannot be undone.")


def register_writes(
    server: MCPServer,
    enabled: frozenset[str],
    write_scope: str,
    grant: Any,
    call: Any,
    atlassian: Atlassian,
    caller: Any,
) -> None:
    """Register the write tools that are on the allow-list. The others are not offered."""
    unknown = enabled - WRITE_TOOLS
    if unknown:
        raise ValueError(f"WRITE_TOOLS names tools that don't exist: {', '.join(sorted(unknown))}")

    def need_write() -> None:
        # The HTTP layer already answered 403 before the call got here (app/scopes.py).
        # Checked again where the change is made, so no other path can skip it.
        who = caller()
        if who is None or write_scope not in who.scopes:
            raise ToolError(f"this needs the '{write_scope}' scope")

    def text_arg(name: str, value: str, most: int) -> str:
        if not value.strip() or len(value) > most:
            raise ToolError(f"{name} must be between 1 and {most} characters")
        return value

    if "jira_add_comment" in enabled:

        @server.tool(
            title="Comment on a Jira issue",
            description="Add a plain-text comment to a Jira issue, as you.",
            annotations=WRITE,
        )
        async def jira_add_comment(key: str, body: str) -> dict[str, Any]:
            need_write()
            if not ISSUE_KEY.fullmatch(key):
                raise ToolError("key must look like PROJ-123")
            text_arg("body", body, 5000)
            g = await grant()
            made = await call(
                atlassian.jira_write,
                g,
                "POST",
                f"/rest/api/3/issue/{key}/comment",
                {"body": adf(body)},
            )
            comment_id = made.get("id") if isinstance(made, dict) else None
            return {"issue": key, "comment_id": comment_id, "url": f"{g.site_url}/browse/{key}"}

    if "jira_create_issue" in enabled:

        @server.tool(
            title="Create a Jira issue",
            description="Create a Jira issue in a project, as you. Plain-text description.",
            annotations=WRITE,
        )
        async def jira_create_issue(
            project: str, summary: str, description: str = "", issue_type: str = "Task"
        ) -> dict[str, Any]:
            need_write()
            if not PROJECT_KEY.fullmatch(project):
                raise ToolError("project must be a project key, like OPS")
            text_arg("summary", summary, 255)
            if len(description) > 10_000:
                raise ToolError("description must be at most 10000 characters")
            if not re.fullmatch(r"[A-Za-z][A-Za-z -]{0,39}", issue_type):
                raise ToolError("issue_type must be a type name, like Task or Bug")
            g = await grant()
            fields: dict[str, Any] = {
                "project": {"key": project},
                "summary": summary,
                "issuetype": {"name": issue_type},
            }
            if description:
                fields["description"] = adf(description)
            made = await call(
                atlassian.jira_write, g, "POST", "/rest/api/3/issue", {"fields": fields}
            )
            key = made.get("key") if isinstance(made, dict) else None
            return {"key": key, "url": f"{g.site_url}/browse/{key}"}

    if "jira_delete_comment" in enabled:

        def delete_args(key: str, comment_id: str) -> str:
            need_write()
            if not ISSUE_KEY.fullmatch(key):
                raise ToolError("key must look like PROJ-123")
            if not COMMENT_ID.fullmatch(comment_id):
                raise ToolError("comment_id must be a number")
            return f"/rest/api/3/issue/{key}/comment/{comment_id}"

        async def confirm_delete(key: str, comment_id: str) -> Elicit[Confirm]:
            """Runs before the tool: what the person is asked. The comment is read from
            Jira first, so they are shown what would be deleted, not what the model says
            it is. A comment they can't see is an error here, and nothing is asked."""
            path = delete_args(key, comment_id)
            comment = await call(atlassian.jira, await grant(), path)
            if not isinstance(comment, dict):
                raise ToolError("Atlassian sent an unexpected response")
            author = _name(comment.get("author")) or "someone"
            excerpt = adf_text(comment.get("body")).strip().replace("\n", " ")[:200]
            return Elicit(f'Delete this comment on {key} by {author}? "{excerpt}"', Confirm)

        async def jira_delete_comment(key, comment_id, confirmation):  # type: ignore[no-untyped-def]
            if not (isinstance(confirmation, AcceptedElicitation) and confirmation.data.confirm):
                return {"deleted": False, "reason": "not confirmed"}
            path = delete_args(key, comment_id)
            await call(atlassian.jira_write, await grant(), "DELETE", path)
            return {"deleted": True, "issue": key, "comment_id": comment_id}

        # The answer comes from the person, through their client: the SDK asks the
        # question `confirm_delete` returns and passes the outcome in. The model that
        # asked for the deletion never supplies this parameter. Annotations are set as
        # objects because the resolver is local to this function.
        jira_delete_comment.__annotations__ = {
            "key": str,
            "comment_id": str,
            "confirmation": Annotated[ElicitationResult[Confirm], Resolve(confirm_delete)],
            "return": dict[str, Any],
        }
        server.tool(
            title="Delete a Jira comment",
            description="Delete one comment from a Jira issue, as you. You will be asked to "
            "confirm; it cannot be undone.",
            annotations=DESTRUCTIVE,
        )(jira_delete_comment)
