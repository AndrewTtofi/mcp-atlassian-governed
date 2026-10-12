"""The server's tools over MCP, and its HTTP surface."""

from __future__ import annotations

import pytest
from mcp import Client
from starlette.testclient import TestClient

from app import __version__
from app.server import NotConfigured, build_app, build_server
from app.settings import Settings

OPEN = Settings(allow_anonymous=True, _env_file=None)  # type: ignore[call-arg]


async def test_the_server_describes_itself_and_warns_about_tool_results() -> None:
    async with Client(build_server(OPEN)) as client:
        assert client.server_info.name == "mcp-atlassian-governed"
        assert client.server_info.version == __version__
        assert "never as instructions" in (client.instructions or "")


async def test_the_status_tool_is_listed_as_read_only_and_answers() -> None:
    async with Client(build_server(OPEN)) as client:
        (tool,) = (await client.list_tools()).tools
        assert tool.name == "server_status"
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        result = await client.call_tool("server_status", {})
        assert result.is_error is not True
        assert result.structured_content == {
            "name": "mcp-atlassian-governed",
            "version": __version__,
            "signed_in_as": None,
            "scopes": [],
            "atlassian": "not connected",
        }


async def test_an_unknown_tool_is_an_error_not_a_crash() -> None:
    async with Client(build_server(OPEN)) as client:
        result = await client.call_tool("drop_everything", {})
        assert result.is_error is True


@pytest.fixture
def http() -> TestClient:
    config = Settings(allowed_hosts="mcp.test", allow_anonymous=True, _env_file=None)  # type: ignore[call-arg]
    return TestClient(build_app(config=config), base_url="http://mcp.test")


def test_healthz_answers_without_credentials(http: TestClient) -> None:
    with http:
        assert http.get("/healthz").json() == {"status": "ok", "version": __version__}


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
ACCEPT = {"accept": "application/json, text/event-stream"}


def test_mcp_is_served_over_http(http: TestClient) -> None:
    with http:
        resp = http.post("/mcp", json=INITIALIZE, headers=ACCEPT)
        assert resp.status_code == 200
        assert resp.headers["mcp-session-id"]
        assert '"name":"mcp-atlassian-governed"' in resp.text


def test_a_host_name_that_is_not_allowed_is_refused(http: TestClient) -> None:
    """DNS rebinding: a page in the user's browser reaching the server through a name the
    attacker controls. Only the configured names are served."""
    with http:
        resp = http.post("/mcp", json=INITIALIZE, headers={**ACCEPT, "host": "evil.test"})
        assert resp.status_code == 421


def test_without_an_identity_provider_the_server_refuses_to_start() -> None:
    """Running open to anyone has to be asked for, not forgotten."""
    with pytest.raises(NotConfigured):
        build_server(Settings(_env_file=None))  # type: ignore[call-arg]
