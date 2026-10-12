"""Governed by the gateway's policy: every tool call is a question to it first
(decision record 0006)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer

from app import tools
from app.atlassian import Atlassian, Grants
from app.audit import Audit, RateLimiter
from app.gateway import GatewayPolicy
from app.linking import Linking
from app.settings import Settings
from app.store import Store, new_key
from app.tokens import JwtVerifier
from tests.test_atlassian import grant
from tests.test_audit import rows
from tests.test_auth import AUDIENCE, ISSUER, RESOURCE, Idp
from tests.test_writes import ALL_WRITES, READ, WRITE, WritableAtlassian


class Gateway:
    """The gateway's POST /v1/authorize, and Atlassian, behind one transport."""

    def __init__(self, atlassian: WritableAtlassian) -> None:
        self.atlassian = atlassian
        self.questions: list[dict[str, Any]] = []
        self.keys: list[str] = []
        self.answer: Any = {"allowed": True, "code": "ok", "reason": "ok", "policy_version": "p1"}
        self.status = 200
        self.down = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host != "gateway.test":
            return self.atlassian(request)
        if self.down:
            raise httpx2.ConnectError("gateway down")
        self.questions.append(json.loads(request.content))
        self.keys.append(request.headers["authorization"])
        if isinstance(self.answer, str):
            return httpx2.Response(self.status, text=self.answer)
        return httpx2.Response(self.status, json=self.answer)


@pytest.fixture
def fake() -> WritableAtlassian:
    return WritableAtlassian(Idp())


@pytest.fixture
def gateway(fake: WritableAtlassian) -> Gateway:
    return Gateway(fake)


@pytest.fixture
def store() -> Store:
    made = Store(":memory:", new_key())
    asyncio.run(made.save_grant(grant()))
    return made


def server(gateway: Gateway, store: Store, subject: str | None = "user-alice") -> MCPServer:
    transport = httpx2.MockTransport(gateway)
    config = Settings(_env_file=None, oidc_issuer=ISSUER, oidc_audience=AUDIENCE)  # type: ignore[call-arg]
    atlassian = Atlassian("id", "secret", "http://mcp.test/cb", "s", transport)
    who = AccessToken(token="user-token", client_id="c", scopes=[READ, WRITE], subject=subject)
    policy = GatewayPolicy(
        "http://gateway.test/", "gw_server_key", "atlassian", "internal", transport
    )
    audit = Audit(
        store, tools.KINDS, RateLimiter(1000), RateLimiter(1000), who=lambda: who, policy=policy
    )
    made = MCPServer(name="test", middleware=[audit])

    @made.tool()
    def server_status() -> dict[str, str]:
        return {"ok": "yes"}

    tools.register(
        made,
        Grants(store, atlassian),
        atlassian,
        Linking(config, store, atlassian, JwtVerifier(ISSUER, AUDIENCE, RESOURCE)),
        write_tools=ALL_WRITES,
        who=lambda: who,
    )
    return made


ISSUE = {"key": "OPS-12"}


async def test_an_allowed_call_asks_the_gateway_first_without_the_users_token(
    gateway: Gateway, store: Store, fake: WritableAtlassian
) -> None:
    async with Client(server(gateway, store)) as client:
        assert (await client.call_tool("jira_get_issue", ISSUE)).is_error is not True
    assert gateway.questions == [
        {
            "subject": "user-alice",
            "connector": "atlassian",
            "tool": "jira_get_issue",
            "data_class": "internal",
        }
    ]
    # The server's own credential. The user's token goes nowhere.
    assert gateway.keys == ["Bearer gw_server_key"]
    assert "user-token" not in json.dumps(gateway.questions)
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    ("code", "says"),
    [
        ("role_not_allowed", "policy doesn't allow"),
        ("tier_too_low", "policy doesn't allow"),
        ("user_deactivated", "deactivated"),
        ("user_not_provisioned", "service desk"),
        ("data_class_not_eligible", "this sensitive"),
    ],
)
async def test_a_refusal_runs_nothing_and_is_recorded_with_the_gateways_code(
    gateway: Gateway, store: Store, fake: WritableAtlassian, code: str, says: str
) -> None:
    gateway.answer = {"allowed": False, "code": code, "reason": "internal detail"}
    async with Client(server(gateway, store)) as client:
        result = await client.call_tool("jira_add_comment", {"key": "OPS-12", "body": "x"})
    assert result.is_error is True and says in str(result.content)
    assert "internal detail" not in str(result.content)  # the gateway's reason stays there
    assert not fake.requests  # Atlassian was never called
    assert await rows(store) == [("user-alice", "tool:jira_add_comment", "write", "refused", code)]


@pytest.mark.parametrize(
    "broken",
    [
        {"down": True},
        {"status": 500},
        {"status": 403},  # this server's own key may not ask
        {"answer": "<html>"},
        {"answer": {"code": "ok"}},  # no `allowed`
        {"answer": {"allowed": "yes", "code": "ok"}},  # not a boolean true
        {"answer": [True]},
    ],
)
async def test_anything_but_a_clear_yes_is_a_no(
    gateway: Gateway, store: Store, fake: WritableAtlassian, broken: dict[str, Any]
) -> None:
    for name, value in broken.items():
        setattr(gateway, name, value)
    async with Client(server(gateway, store)) as client:
        result = await client.call_tool("jira_get_issue", ISSUE)
    assert result.is_error is True and not fake.requests
    outcome = (await rows(store))[-1]
    assert (outcome[3], outcome[4]) == ("refused", "gateway_unavailable")
    assert "policy" in str(result.content).lower()


async def test_an_unknown_tool_or_an_unidentified_caller_is_not_asked_about(
    gateway: Gateway, store: Store
) -> None:
    async with Client(server(gateway, store)) as client:
        assert (await client.call_tool("drop_all_tables", {})).is_error is True
    async with Client(server(gateway, store, subject=None)) as client:
        assert (await client.call_tool("jira_get_issue", ISSUE)).is_error is True
    assert gateway.questions == []
    assert [r[4] for r in await rows(store)] == ["not_identified", "not_identified"]


async def test_the_status_tool_is_not_a_policy_question(gateway: Gateway, store: Store) -> None:
    gateway.down = True
    async with Client(server(gateway, store)) as client:
        assert (await client.call_tool("server_status", {})).is_error is not True
    assert gateway.questions == []
