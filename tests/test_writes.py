"""Tools that change things: the allow-list, the write scope, and the confirmation a
person gives before anything is deleted (decision record 0004)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from mcp import Client, MCPError, types
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from app import tools
from app.atlassian import Atlassian, Grants
from app.linking import Linking
from app.scopes import called_tools
from app.server import build_app, build_server
from app.settings import Settings
from app.store import Store, new_key
from app.tokens import JwtVerifier
from tests.test_atlassian import FakeAtlassian, Session, grant, text
from tests.test_auth import AUDIENCE, ISSUER, RESOURCE, Clock, Idp

READ, WRITE = "atlassian:read", "atlassian:write"
ALL_WRITES = frozenset(tools.WRITE_TOOLS)
COMMENT = {
    "id": "9001",
    "author": {"displayName": "Bob Example"},
    "body": {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Ship it Friday."}]}
        ],
    },
}


class WritableAtlassian(FakeAtlassian):
    """The stand-in, with the endpoints that change things."""

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path, method = request.url.path, request.method
        if request.url.host == "api.atlassian.com" and self.api_status == 200:
            if method == "POST" and path.endswith("/issue/OPS-12/comment"):
                self.requests.append(request)
                return httpx2.Response(201, json={"id": "9002"})
            if method == "POST" and path.endswith("/rest/api/3/issue"):
                self.requests.append(request)
                return httpx2.Response(201, json={"id": "10", "key": "OPS-13"})
            if path.endswith("/issue/OPS-12/comment/9001"):
                self.requests.append(request)
                if method == "DELETE":
                    return httpx2.Response(204)
                return httpx2.Response(200, json=COMMENT)
        return super().__call__(request)

    def sent(self, method: str) -> list[httpx2.Request]:
        return [
            r for r in self.requests if r.method == method and r.url.host == "api.atlassian.com"
        ]


@pytest.fixture
def idp() -> Idp:
    return Idp()


@pytest.fixture
def fake(idp: Idp) -> WritableAtlassian:
    return WritableAtlassian(idp)


@pytest.fixture
def store() -> Store:
    store = Store(":memory:", new_key())
    asyncio.run(store.save_grant(grant()))
    return store


def server(
    fake: WritableAtlassian,
    store: Store,
    enabled: frozenset[str] = ALL_WRITES,
    scopes: tuple[str, ...] = (READ, WRITE),
) -> MCPServer:
    """A server whose caller is alice with the given scopes, for the in-process client."""
    config = Settings(_env_file=None, oidc_issuer=ISSUER, oidc_audience=AUDIENCE)  # type: ignore[call-arg]
    atlassian = Atlassian("id", "secret", "http://mcp.test/cb", "s", httpx2.MockTransport(fake))
    verifier = JwtVerifier(ISSUER, AUDIENCE, RESOURCE)
    made = MCPServer(name="test")
    who = AccessToken(token="t", client_id="c", scopes=list(scopes), subject="user-alice")
    tools.register(
        made,
        Grants(store, atlassian),
        atlassian,
        Linking(config, store, atlassian, verifier),
        write_tools=enabled,
        who=lambda: who,
    )
    return made


def answer(action: str, confirm: bool | None = None) -> Any:
    """A client whose user answers a confirmation this way. → (callback, what was asked)"""
    asked: list[str] = []

    async def callback(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        asked.append(params.message)
        content = None if confirm is None else {"confirm": confirm}
        return types.ElicitResult(action=action, content=content)  # type: ignore[arg-type]

    return callback, asked


# --- the allow-list --------------------------------------------------------------------


async def test_write_tools_exist_only_when_they_are_on_the_allow_list(
    fake: WritableAtlassian, store: Store
) -> None:
    async with Client(server(fake, store, enabled=frozenset())) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert not names & tools.WRITE_TOOLS  # a read-only server
        assert (await client.call_tool("jira_add_comment", {"key": "OPS-12", "body": "x"})).is_error
    async with Client(server(fake, store, enabled=frozenset({"jira_add_comment"}))) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names & tools.WRITE_TOOLS == {"jira_add_comment"}
    assert not fake.sent("POST")


def test_an_allow_list_naming_a_tool_that_does_not_exist_fails_at_start(
    fake: WritableAtlassian, store: Store
) -> None:
    with pytest.raises(ValueError, match="jira_delete_everything"):
        server(fake, store, enabled=frozenset({"jira_delete_everything"}))


async def test_tools_say_what_kind_they_are(fake: WritableAtlassian, store: Store) -> None:
    async with Client(server(fake, store)) as client:
        hints = {t.name: t.annotations for t in (await client.list_tools()).tools}
    assert hints["jira_search"].read_only_hint is True  # type: ignore[union-attr]
    assert hints["jira_add_comment"].read_only_hint is False  # type: ignore[union-attr]
    assert hints["jira_add_comment"].destructive_hint is False  # type: ignore[union-attr]
    assert hints["jira_delete_comment"].destructive_hint is True  # type: ignore[union-attr]


# --- writing ---------------------------------------------------------------------------


async def test_a_comment_is_added_as_the_user_in_plain_text(
    fake: WritableAtlassian, store: Store
) -> None:
    async with Client(server(fake, store)) as client:
        body = "Done.\n[~accountid:admin] <script>alert(1)</script>"
        result = await client.call_tool("jira_add_comment", {"key": "OPS-12", "body": body})
    assert result.structured_content == {
        "issue": "OPS-12",
        "comment_id": "9002",
        "url": "https://acme.atlassian.net/browse/OPS-12",
    }
    (sent,) = fake.sent("POST")
    assert sent.url.path == "/ex/jira/cloud-1/rest/api/3/issue/OPS-12/comment"
    assert sent.headers["authorization"] == "Bearer at-secret-value"  # alice's own grant
    doc = json.loads(sent.content)["body"]
    # Two paragraphs of text nodes: nothing the model wrote became a mention or markup.
    assert [p["content"][0]["type"] for p in doc["content"]] == ["text", "text"]
    assert doc["content"][1]["content"][0]["text"] == "[~accountid:admin] <script>alert(1)</script>"


async def test_an_issue_is_created(fake: WritableAtlassian, store: Store) -> None:
    async with Client(server(fake, store)) as client:
        result = await client.call_tool(
            "jira_create_issue",
            {"project": "OPS", "summary": "Renew the certificate", "issue_type": "Bug"},
        )
    assert result.structured_content == {
        "key": "OPS-13",
        "url": "https://acme.atlassian.net/browse/OPS-13",
    }
    fields = json.loads(fake.sent("POST")[0].content)["fields"]
    assert fields == {
        "project": {"key": "OPS"},
        "summary": "Renew the certificate",
        "issuetype": {"name": "Bug"},
    }


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("jira_add_comment", {"key": "OPS-12/../13", "body": "x"}),
        ("jira_add_comment", {"key": "OPS-12", "body": " "}),
        ("jira_add_comment", {"key": "OPS-12", "body": "x" * 5001}),
        ("jira_create_issue", {"project": "ops", "summary": "x"}),
        ("jira_create_issue", {"project": "OPS/../ADMIN", "summary": "x"}),
        ("jira_create_issue", {"project": "OPS", "summary": ""}),
        ("jira_create_issue", {"project": "OPS", "summary": "x", "issue_type": 'Task"}'}),
        ("jira_delete_comment", {"key": "OPS-12", "comment_id": "9001/../9"}),
    ],
)
async def test_bad_arguments_change_nothing(
    fake: WritableAtlassian, store: Store, tool: str, arguments: dict[str, Any]
) -> None:
    callback, _ = answer("accept", True)
    async with Client(server(fake, store), elicitation_callback=callback) as client:
        assert (await client.call_tool(tool, arguments)).is_error is True
    assert not fake.sent("POST") and not fake.sent("DELETE")


async def test_without_the_write_scope_nothing_is_changed(
    fake: WritableAtlassian, store: Store
) -> None:
    async with Client(server(fake, store, scopes=(READ,))) as client:
        result = await client.call_tool("jira_add_comment", {"key": "OPS-12", "body": "x"})
        assert result.is_error is True and WRITE in str(result.content)
        assert (await client.call_tool("jira_get_issue", {"key": "OPS-12"})).is_error is not True
    assert not fake.sent("POST")


# --- deleting needs a person -----------------------------------------------------------

DELETE = {"key": "OPS-12", "comment_id": "9001"}


async def test_a_confirmed_deletion_shows_the_person_the_real_comment(
    fake: WritableAtlassian, store: Store
) -> None:
    callback, asked = answer("accept", True)
    async with Client(server(fake, store), elicitation_callback=callback) as client:
        result = await client.call_tool("jira_delete_comment", DELETE)
    assert result.structured_content == {"deleted": True, "issue": "OPS-12", "comment_id": "9001"}
    # What the person was asked came from Jira, not from the model's description of it.
    assert asked == ['Delete this comment on OPS-12 by Bob Example? "Ship it Friday."']
    assert [r.url.path for r in fake.sent("DELETE")] == [
        "/ex/jira/cloud-1/rest/api/3/issue/OPS-12/comment/9001"
    ]


@pytest.mark.parametrize(
    ("action", "confirm"), [("decline", None), ("cancel", None), ("accept", False)]
)
async def test_anything_but_yes_deletes_nothing(
    fake: WritableAtlassian, store: Store, action: str, confirm: bool | None
) -> None:
    callback, asked = answer(action, confirm)
    async with Client(server(fake, store), elicitation_callback=callback) as client:
        result = await client.call_tool("jira_delete_comment", DELETE)
    assert result.structured_content == {"deleted": False, "reason": "not confirmed"}
    assert len(asked) == 1 and not fake.sent("DELETE")


async def test_a_client_that_cannot_ask_the_person_cannot_delete(
    fake: WritableAtlassian, store: Store
) -> None:
    async with Client(server(fake, store)) as client:  # no way to ask
        with pytest.raises(MCPError, match="elicitation"):
            await client.call_tool("jira_delete_comment", DELETE)
    assert not fake.sent("DELETE")


async def test_a_comment_the_user_cannot_see_is_never_asked_about(
    fake: WritableAtlassian, store: Store
) -> None:
    fake.api_status = 404
    callback, asked = answer("accept", True)
    async with Client(server(fake, store), elicitation_callback=callback) as client:
        assert (await client.call_tool("jira_delete_comment", DELETE)).is_error is True
    assert asked == []


# --- over HTTP: the 403 that names the scope -------------------------------------------


def test_tool_names_are_read_from_a_request_body() -> None:
    def call(name: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}

    assert called_tools(json.dumps(call("a")).encode()) == {"a"}
    assert called_tools(json.dumps([call("a"), call("b")]).encode()) == {"a", "b"}
    assert called_tools(b'{"method": "tools/list"}') == set()
    assert called_tools(b"not json") == set() and called_tools(b'{"params": 5}') == set()
    assert called_tools(b'{"method": "tools/call", "params": {"name": 5}}') == set()


@pytest.fixture
def http(idp: Idp, fake: WritableAtlassian, store: Store) -> TestClient:
    config = Settings(  # type: ignore[call-arg]
        _env_file=None,
        allowed_hosts="mcp.test",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_authorize_url=f"{ISSUER}authorize/",
        oidc_token_url=f"{ISSUER}token/",
        resource_url=RESOURCE,
        public_url="http://mcp.test",
        atlassian_client_id="client-id",
        atlassian_client_secret="client-secret",
        write_tools="jira_add_comment,jira_create_issue,jira_delete_comment",
    )
    verifier = JwtVerifier(ISSUER, AUDIENCE, RESOURCE, fetch=idp.fetch, clock=Clock())
    built = build_server(config, verifier, store, httpx2.MockTransport(fake))
    return TestClient(build_app(built, config), base_url="http://mcp.test")


def test_a_read_only_token_calling_a_write_tool_gets_a_403_that_names_the_scope(
    http: TestClient, idp: Idp, fake: WritableAtlassian
) -> None:
    with http:
        reader = Session(http, idp.token(scope=f"openid {READ}"))
        assert reader.call("jira_get_issue", key="OPS-12").get("isError") is not True
        body = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "jira_add_comment", "arguments": {"key": "OPS-12", "body": "x"}},
        }
        resp = http.post("/mcp", json=body, headers=reader.headers)
        assert resp.status_code == 403
        challenge = resp.headers["www-authenticate"]
        assert 'error="insufficient_scope"' in challenge
        assert f'scope="{READ} {WRITE}"' in challenge  # the union: the new token still reads
        assert "resource_metadata=" in challenge
        assert not fake.sent("POST")


def test_a_token_with_the_write_scope_writes(
    http: TestClient, idp: Idp, fake: WritableAtlassian
) -> None:
    with http:
        writer = Session(http, idp.token(scope=f"openid {READ} {WRITE}"))
        result = writer.call("jira_add_comment", key="OPS-12", body="On it.")
        assert result.get("isError") is not True, text(result)
        assert len(fake.sent("POST")) == 1


def test_no_token_on_a_write_tool_is_still_a_401(http: TestClient) -> None:
    with http:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "jira_add_comment"},
        }
        resp = http.post(
            "/mcp", json=body, headers={"accept": "application/json, text/event-stream"}
        )
        assert resp.status_code == 401
