"""The audit record and the per-user rate limit (decision record 0005)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from app import tools
from app.atlassian import Atlassian, Grants
from app.audit import Audit, RateLimiter
from app.linking import Linking
from app.settings import Settings
from app.store import Store, new_key
from app.tokens import JwtVerifier
from tests.test_atlassian import Session, browse, grant, link_url
from tests.test_auth import AUDIENCE, ISSUER, RESOURCE, Clock, Idp
from tests.test_writes import ALL_WRITES, DELETE, READ, WRITE, WritableAtlassian, answer
from tests.test_writes import http as http  # the HTTP app with write tools enabled

SECRET_TEXT = "the-merger-is-on-friday"


@pytest.fixture
def idp() -> Idp:
    return Idp()


@pytest.fixture
def fake(idp: Idp) -> WritableAtlassian:
    return WritableAtlassian(idp)


@pytest.fixture
def store() -> Store:
    made = Store(":memory:", new_key())
    asyncio.run(made.save_grant(grant()))
    return made


def server(
    fake: WritableAtlassian,
    store: Store,
    per_minute: int = 1000,
    writes_per_minute: int = 1000,
    clock: Clock | None = None,
) -> MCPServer:
    """An audited server whose caller is alice, for the in-process client."""
    config = Settings(_env_file=None, oidc_issuer=ISSUER, oidc_audience=AUDIENCE)  # type: ignore[call-arg]
    atlassian = Atlassian("id", "secret", "http://mcp.test/cb", "s", httpx2.MockTransport(fake))
    who = AccessToken(token="t", client_id="c", scopes=[READ, WRITE], subject="user-alice")
    limits = {"clock": clock} if clock else {}
    audit = Audit(
        store,
        tools.KINDS,
        RateLimiter(per_minute, **limits),  # type: ignore[arg-type]
        RateLimiter(writes_per_minute, **limits),  # type: ignore[arg-type]
        who=lambda: who,
    )
    made = MCPServer(name="test", middleware=[audit])
    tools.register(
        made,
        Grants(store, atlassian),
        atlassian,
        Linking(config, store, atlassian, JwtVerifier(ISSUER, AUDIENCE, RESOURCE)),
        write_tools=ALL_WRITES,
        who=lambda: who,
    )
    return made


async def rows(store: Store) -> list[tuple[str, str, str, str, str | None]]:
    """Oldest first: (subject, event, kind, outcome, detail)."""
    found = await store.audit_rows()
    return [(r["subject"], r["event"], r["kind"], r["outcome"], r["detail"]) for r in found][::-1]


async def test_every_call_is_recorded_with_how_it_ended(
    fake: WritableAtlassian, store: Store
) -> None:
    async with Client(server(fake, store)) as client:
        await client.call_tool("jira_get_issue", {"key": "OPS-12"})
        await client.call_tool("jira_get_issue", {"key": "not a key"})
        await client.call_tool("jira_add_comment", {"key": "OPS-12", "body": "On it."})
        await client.call_tool("drop_all_tables", {})
        await client.list_tools()  # not a tool call: not recorded
    assert await rows(store) == [
        ("user-alice", "tool:jira_get_issue", "read", "ok", None),
        ("user-alice", "tool:jira_get_issue", "read", "error", None),
        ("user-alice", "tool:jira_add_comment", "write", "ok", None),
        # A name the server doesn't know is client input: it is not written down.
        ("user-alice", "tool:unknown", "read", "error", None),
    ]
    assert all(isinstance(r["ms"], int) for r in await store.audit_rows())


async def test_a_deletion_records_the_question_and_the_answer(
    fake: WritableAtlassian, store: Store
) -> None:
    yes, _ = answer("accept", True)
    async with Client(server(fake, store), elicitation_callback=yes) as client:
        await client.call_tool("jira_delete_comment", DELETE)
    no, _ = answer("decline")
    async with Client(server(fake, store), elicitation_callback=no) as client:
        await client.call_tool("jira_delete_comment", DELETE)
    outcomes = [(event, kind, outcome) for _, event, kind, outcome, _ in await rows(store)]
    assert outcomes == [
        ("tool:jira_delete_comment", "destructive", "asked"),
        ("tool:jira_delete_comment", "destructive", "ok"),
        ("tool:jira_delete_comment", "destructive", "asked"),
        ("tool:jira_delete_comment", "destructive", "not_confirmed"),
    ]


async def test_the_record_never_holds_what_was_said(
    fake: WritableAtlassian, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp.audit")
    async with Client(server(fake, store)) as client:
        await client.call_tool("jira_add_comment", {"key": "OPS-12", "body": SECRET_TEXT})
        await client.call_tool("jira_search", {"jql": f'text ~ "{SECRET_TEXT}"'})
        await client.call_tool(SECRET_TEXT, {})
    stored = json.dumps(await store.audit_rows())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for where in (stored, logged):
        assert SECRET_TEXT not in where and "OPS-12" not in where
        assert "at-secret-value" not in where and "Rotate the signing key" not in where
    line = json.loads(caplog.records[0].getMessage())
    assert line == {
        "subject": "user-alice",
        "event": "tool:jira_add_comment",
        "kind": "write",
        "outcome": "ok",
        "detail": None,
        "ms": line["ms"],
    }


async def test_a_failure_at_atlassian_is_recorded_as_an_error(
    fake: WritableAtlassian, store: Store
) -> None:
    fake.api_status = 500
    async with Client(server(fake, store)) as client:
        assert (await client.call_tool("jira_get_issue", {"key": "OPS-12"})).is_error
    assert (await rows(store))[-1][3] == "error"


async def test_the_record_survives_the_database_failing(
    fake: WritableAtlassian, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp.audit")
    made = server(fake, store)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    store.audit = broken  # type: ignore[method-assign]
    async with Client(made) as client:
        result = await client.call_tool("jira_get_issue", {"key": "OPS-12"})
    assert result.is_error is not True  # the call is not failed by its own bookkeeping
    assert any('"event":"tool:jira_get_issue"' in r.getMessage() for r in caplog.records)


# --- rate limits -----------------------------------------------------------------------


def test_the_window_slides() -> None:
    clock = Clock()
    limiter = RateLimiter(3, clock=clock)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("b") is True  # per user
    clock.now = 59
    assert limiter.allow("a") is False
    clock.now = 61
    assert limiter.allow("a") is True


async def test_too_many_calls_are_refused_and_recorded(
    fake: WritableAtlassian, store: Store
) -> None:
    clock = Clock()
    async with Client(server(fake, store, per_minute=2, clock=clock)) as client:
        results = [await client.call_tool("jira_get_issue", {"key": "OPS-12"}) for _ in range(3)]
        assert [bool(r.is_error) for r in results] == [False, False, True]
        assert "Too many calls" in str(results[2].content)
        calls_to_atlassian = len(fake.requests)
        clock.now = 61
        assert (await client.call_tool("jira_get_issue", {"key": "OPS-12"})).is_error is not True
    assert calls_to_atlassian == 2  # the refused call never reached Atlassian
    assert [r[3] for r in await rows(store)] == ["ok", "ok", "rate_limited", "ok"]


async def test_writes_have_their_own_smaller_limit(fake: WritableAtlassian, store: Store) -> None:
    async with Client(server(fake, store, writes_per_minute=1)) as client:
        comment = {"key": "OPS-12", "body": "x"}
        assert (await client.call_tool("jira_add_comment", comment)).is_error is not True
        assert (await client.call_tool("jira_add_comment", comment)).is_error is True
        assert (await client.call_tool("jira_get_issue", {"key": "OPS-12"})).is_error is not True
    assert len(fake.sent("POST")) == 1


# --- what happens outside a tool call ---------------------------------------------------


def test_linking_and_scope_refusals_are_recorded(
    http: TestClient, idp: Idp, fake: WritableAtlassian, store: Store
) -> None:
    asyncio.run(store.delete_grant("user-alice"))
    with http:
        alice = Session(http, idp.token(scope=f"openid {READ}"))
        start = link_url(alice.call("jira_search", jql="project = OPS"))
        assert browse(http, fake, start).status_code == 200
        mallory = Session(http, idp.token(sub="user-mallory"))
        theirs = link_url(mallory.call("jira_search", jql="project = OPS"))
        assert browse(http, fake, theirs).status_code == 403  # opened as alice
        body = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "jira_add_comment", "arguments": {"key": "OPS-12", "body": "x"}},
        }
        assert http.post("/mcp", json=body, headers=alice.headers).status_code == 403
    found = asyncio.run(rows(store))
    assert ("user-alice", "link", "account", "ok", "linked") in found
    assert ("user-mallory", "link", "account", "refused", "opened_by_another_user") in found
    assert (
        "user-alice",
        "tool:jira_add_comment",
        "write",
        "refused",
        "insufficient_scope",
    ) in found


def test_old_records_are_deleted_at_start(tmp_path: Any) -> None:
    clock, key, path = Clock(), new_key(), str(tmp_path / "mcp.sqlite3")
    clock.now = 1_000_000.0
    first = Store(path, key, clock=clock)
    asyncio.run(first.audit("user-alice", "tool:jira_search", "read", "ok"))
    first.close()
    clock.now += 300 * 86400
    kept = Store(path, key, clock=clock, audit_retention_days=365)
    assert len(asyncio.run(kept.audit_rows())) == 1
    kept.close()
    clock.now += 100 * 86400
    pruned = Store(path, key, clock=clock, audit_retention_days=365)
    assert asyncio.run(pruned.audit_rows()) == []
