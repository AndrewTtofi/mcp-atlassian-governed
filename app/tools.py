"""The Jira and Confluence tools. Each runs with the calling user's own Atlassian grant.

What comes back is trimmed to a few fields and bounded in size, and it is content other
people wrote: the server's instructions tell the model to treat it as information only.
"""

from __future__ import annotations

import html
import re
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.atlassian import Atlassian, AtlassianError, Grants, NotLinked
from app.linking import Linking
from app.store import Grant

ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,19}-[0-9]{1,9}")
PAGE_ID = re.compile(r"[0-9]{1,20}")
MAX_RESULTS = 25
TEXT_CHARS = 6000  # a description or page body is cut here
READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
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


def register(server: MCPServer, grants: Grants, atlassian: Atlassian, linking: Linking) -> None:
    async def grant() -> Grant:
        """The caller's grant, or an error that tells them how to link their account."""
        caller = get_access_token()
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
            caller = get_access_token()
            if caller and caller.subject:
                await grants.forget(caller.subject)
            raise ToolError(
                "your Atlassian link is no longer valid; call the tool again to get a new link"
            ) from None
        except AtlassianError as exc:
            raise ToolError(str(exc)) from None

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
