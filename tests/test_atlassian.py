"""Atlassian account linking, the encrypted store, token refresh and the read tools
(decision record 0003). Atlassian and the identity provider are stand-ins."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from starlette.testclient import TestClient

from app.atlassian import Atlassian, AtlassianError, Grants, NotLinked
from app.server import build_app, build_server
from app.settings import Settings
from app.store import LINK_SECONDS, Grant, Store, StoreError, new_key
from app.tokens import JwtVerifier
from app.tools import adf_text, storage_text
from tests.test_auth import ACCEPT, AUDIENCE, INITIALIZE, ISSUER, RESOURCE, Clock, Idp, bearer

SITE = {"id": "cloud-1", "url": "https://acme.atlassian.net", "name": "acme", "scopes": []}
ISSUE = {
    "key": "OPS-12",
    "fields": {
        "summary": "Rotate the signing key",
        "status": {"name": "In Progress"},
        "assignee": {"displayName": "Alice Example", "accountId": "left out"},
        "reporter": {"displayName": "Bob Example"},
        "priority": {"name": "High"},
        "issuetype": {"name": "Task"},
        "created": "2026-10-01T10:00:00.000+0000",
        "updated": "2026-10-02T10:00:00.000+0000",
        "description": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Rotate it."}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Then tell ops."}]},
            ],
        },
    },
}


class FakeAtlassian:
    """Atlassian's token endpoint and APIs, and the identity provider's token endpoint."""

    def __init__(self, idp: Idp) -> None:
        self.idp = idp
        self.requests: list[httpx2.Request] = []
        self.refresh_tokens = {"rt-1"}  # the ones Atlassian would still accept
        self.issued = 0
        self.api_status = 200
        self.token_status = 200
        self.subject = "user-alice"  # who the browser signs in as
        self.pkce: dict[str, str] = {}  # code → the challenge it was requested with

    def _tokens(self) -> httpx2.Response:
        self.issued += 1
        refresh = f"rt-{self.issued + 1}"
        self.refresh_tokens = {refresh}  # rotating: the old one is disabled
        body = {
            "access_token": f"at-{self.issued}",
            "refresh_token": refresh,
            "expires_in": 3600,
            "scope": "read:jira-work offline_access",
        }
        return httpx2.Response(200, json=body)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{ISSUER}token/":  # the identity provider redeeming a sign-in code
            form = parse_qs(request.content.decode())
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"][0].encode()).digest()
            ).rstrip(b"=")
            if self.pkce.get(form["code"][0]) != challenge.decode():
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(200, json={"id_token": self.idp.token(sub=self.subject)})
        if url == "https://auth.atlassian.com/oauth/token":
            if self.token_status != 200:
                return httpx2.Response(self.token_status, json={"error": "invalid_grant"})
            body = json.loads(request.content)
            refreshing = body["grant_type"] == "refresh_token"
            if refreshing and body["refresh_token"] not in self.refresh_tokens:
                return httpx2.Response(403, json={"error": "invalid_grant"})
            return self._tokens()
        if self.api_status != 200:
            return httpx2.Response(self.api_status, json={"message": "no"})
        path = request.url.path
        if path == "/oauth/token/accessible-resources":
            return httpx2.Response(200, json=[SITE])
        if path == "/me":
            return httpx2.Response(200, json={"name": "Alice Example", "email": "a@example.com"})
        if path.endswith("/rest/api/3/search/jql"):
            return httpx2.Response(200, json={"issues": [ISSUE]})
        if path.endswith("/rest/api/3/issue/OPS-12"):
            return httpx2.Response(200, json=ISSUE)
        if path.endswith("/wiki/rest/api/search"):
            hit = {
                "content": {"id": "4411", "type": "page", "title": "Key rotation runbook"},
                "excerpt": "How to <b>rotate</b> keys &amp; tell ops",
                "url": "/spaces/OPS/pages/4411",
            }
            return httpx2.Response(200, json={"results": [hit, {"user": {}}]})
        if path.endswith("/wiki/rest/api/content/4411"):
            page = {
                "id": "4411",
                "title": "Key rotation runbook",
                "space": {"key": "OPS"},
                "version": {"number": 7},
                "body": {"storage": {"value": "<h1>Steps</h1><p>Rotate &amp; verify.</p>"}},
            }
            return httpx2.Response(200, json=page)
        return httpx2.Response(404, json={"message": "nope"})


@pytest.fixture
def idp() -> Idp:
    return Idp()


@pytest.fixture
def fake(idp: Idp) -> FakeAtlassian:
    return FakeAtlassian(idp)


@pytest.fixture
def store() -> Store:
    return Store(":memory:", new_key())


def grant(**changes: Any) -> Grant:
    base: dict[str, Any] = dict(
        subject="user-alice",
        cloud_id="cloud-1",
        site_url="https://acme.atlassian.net",
        account="Alice Example",
        access_token="at-secret-value",
        refresh_token="rt-1",
        expires_at=time.time() + 3600,
        scopes="read:jira-work",
    )
    return Grant(**{**base, **changes})


# --- the store -------------------------------------------------------------------------


def on_disk(path: str) -> tuple[bytes, bytes, str]:
    """The raw file, and the stored row as SQLite sees it."""
    row = sqlite3.connect(path).execute("SELECT access_enc, account FROM grants").fetchone()
    return Path(path).read_bytes(), row[0], row[1]


async def test_tokens_are_encrypted_in_the_database_file(tmp_path: Path) -> None:
    path = str(tmp_path / "db" / "mcp.sqlite3")
    store = Store(path, new_key())
    await store.save_grant(grant())
    found = await store.grant("user-alice")
    assert found is not None and found.access_token == "at-secret-value"
    store.close()
    raw, stored_token, account = on_disk(path)
    assert b"at-secret-value" not in raw and b"rt-1" not in raw
    assert account == "Alice Example" and b"at-secret-value" not in stored_token


async def test_a_key_is_rotated_without_relinking_and_a_lost_key_means_relinking(
    tmp_path: Path,
) -> None:
    path, old, new = str(tmp_path / "mcp.sqlite3"), new_key(), new_key()
    first = Store(path, old)
    await first.save_grant(grant())
    first.close()
    rotated = Store(path, f"{new},{old}")  # the new key encrypts, the old one still decrypts
    found = await rotated.grant("user-alice")
    assert found is not None and found.access_token == "at-secret-value"
    rotated.close()
    lost = Store(path, new)  # the old key is gone
    assert await lost.grant("user-alice") is None
    assert await lost.grant("user-alice") is None  # and the unusable row was removed


def test_the_store_needs_a_valid_key() -> None:
    for bad in ("", " , ", "not-a-fernet-key"):
        with pytest.raises(StoreError):
            Store(":memory:", bad)


async def test_a_link_is_single_per_user_and_expires() -> None:
    clock = Clock()
    store = Store(":memory:", new_key(), clock=clock)
    first = await store.create_link("user-alice")
    second = await store.create_link("user-alice")
    assert await store.link(first) is None  # a newer one voids it
    link = await store.link(second)
    assert link is not None and (link.subject, link.verified) == ("user-alice", False)
    clock.now = LINK_SECONDS + 1
    assert await store.link(second) is None
    assert await store.link("") is None and await store.link_by_idp_state("") is None


# --- the grant: exchange and refresh ---------------------------------------------------


def client(fake: FakeAtlassian, clock: Clock | None = None) -> Atlassian:
    return Atlassian(
        "client-id",
        "client-secret",
        "http://mcp.test/atlassian/callback",
        "read:jira-work offline_access",
        transport=httpx2.MockTransport(fake),
        **({"clock": clock} if clock else {}),
    )


def test_the_consent_url_asks_for_offline_access_and_always_prompts(fake: FakeAtlassian) -> None:
    url = urlparse(client(fake).authorize_url("state-1"))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert (url.netloc, url.path) == ("auth.atlassian.com", "/authorize")
    assert query == {
        "audience": "api.atlassian.com",
        "client_id": "client-id",
        "scope": "read:jira-work offline_access",
        "redirect_uri": "http://mcp.test/atlassian/callback",
        "state": "state-1",
        "response_type": "code",
        "prompt": "consent",
    }


async def test_an_access_token_about_to_expire_is_refreshed_and_the_new_pair_stored(
    fake: FakeAtlassian, store: Store
) -> None:
    clock = Clock()
    clock.now = 1000.0
    grants = Grants(store, client(fake, clock), clock=clock)
    await store.save_grant(grant(expires_at=clock.now + 3600))
    assert (await grants.current("user-alice")).access_token == "at-secret-value"
    assert fake.issued == 0  # plenty of time left: nothing was asked of Atlassian
    clock.now += 3600 - 30  # inside the last minute
    fresh = await grants.current("user-alice")
    assert (fresh.access_token, fresh.refresh_token) == ("at-1", "rt-2")
    stored = await store.grant("user-alice")
    assert stored is not None and stored.refresh_token == "rt-2"  # the rotated one is kept
    assert (await grants.current("user-alice")).access_token == "at-1" and fake.issued == 1


async def test_concurrent_calls_refresh_once(fake: FakeAtlassian, store: Store) -> None:
    grants = Grants(store, client(fake))
    await store.save_grant(grant(expires_at=time.time() - 5))
    found = await asyncio.gather(*(grants.current("user-alice") for _ in range(10)))
    # A refresh token works once: ten refreshes would have disabled nine callers' tokens.
    assert fake.issued == 1 and {g.access_token for g in found} == {"at-1"}


async def test_a_dead_refresh_token_means_linking_again(fake: FakeAtlassian, store: Store) -> None:
    grants = Grants(store, client(fake))
    await store.save_grant(grant(expires_at=time.time() - 5, refresh_token="rt-revoked"))
    with pytest.raises(NotLinked):
        await grants.current("user-alice")
    assert await store.grant("user-alice") is None  # not tried again
    with pytest.raises(NotLinked):
        await grants.current("user-nobody")


@pytest.mark.parametrize(
    ("status", "error", "says"),
    [
        (401, NotLinked, "no longer valid"),
        (403, AtlassianError, "no access"),
        (404, AtlassianError, "no access"),
        (400, AtlassianError, "rejected the query"),
        (429, AtlassianError, "rate limiting"),
        (500, AtlassianError, "HTTP 500"),
    ],
)
async def test_atlassian_failures_are_errors_safe_to_show(
    fake: FakeAtlassian, status: int, error: type[Exception], says: str
) -> None:
    fake.api_status = status
    with pytest.raises(error) as err:
        await client(fake).jira(grant(), "/rest/api/3/issue/OPS-12")
    assert says in str(err.value) and "at-secret-value" not in str(err.value)


async def test_calls_carry_the_users_own_token_to_their_own_site(fake: FakeAtlassian) -> None:
    await client(fake).jira(grant(), "/rest/api/3/issue/OPS-12", {"fields": "summary"})
    sent = fake.requests[-1]
    assert sent.headers["authorization"] == "Bearer at-secret-value"
    assert str(sent.url) == (
        "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/issue/OPS-12?fields=summary"
    )


def test_rich_text_becomes_plain_text() -> None:
    assert adf_text(ISSUE["fields"]["description"]) == "Rotate it.\nThen tell ops.\n"  # type: ignore[index]
    assert adf_text(None) == "" and adf_text({"type": "mention"}) == ""
    assert storage_text("<h1>Steps</h1><p>Rotate &amp; verify.</p>") == "Steps Rotate & verify."


# --- over HTTP: linking in the browser, then the tools ---------------------------------


@pytest.fixture
def http(idp: Idp, fake: FakeAtlassian, store: Store) -> TestClient:
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
    )
    verifier = JwtVerifier(ISSUER, AUDIENCE, RESOURCE, fetch=idp.fetch, clock=Clock())
    server = build_server(config, verifier, store, httpx2.MockTransport(fake))
    return TestClient(build_app(server, config), base_url="http://mcp.test", follow_redirects=False)


class Session:
    """An MCP session over HTTP as one user."""

    def __init__(self, http: TestClient, token: str) -> None:
        self.http, self.headers, self.n = http, bearer(token), 1
        opened = http.post("/mcp", json=INITIALIZE, headers=self.headers)
        assert opened.status_code == 200, opened.text
        self.headers["mcp-session-id"] = opened.headers["mcp-session-id"]
        http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self.headers,
        )

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        self.n += 1
        body = {
            "jsonrpc": "2.0",
            "id": self.n,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self.http.post("/mcp", json=body, headers=self.headers)
        assert resp.status_code == 200, resp.text
        line = next(x for x in resp.text.splitlines() if x.startswith("data: "))
        return dict(json.loads(line[6:])["result"])


def text(result: dict[str, Any]) -> str:
    return " ".join(c.get("text", "") for c in result.get("content", []))


def link_url(result: dict[str, Any]) -> str:
    assert result.get("isError") is True
    return next(w for w in text(result).split() if w.startswith("http://mcp.test/atlassian/link"))


def browse(
    http: TestClient, fake: FakeAtlassian, start: str, consent: bool = True
) -> httpx2.Response:
    """What a user's browser does with the link: sign in, consent at Atlassian, come back."""
    to_idp = http.get(start.removeprefix("http://mcp.test"))
    assert to_idp.status_code == 303, to_idp.text
    sign_in = {k: v[0] for k, v in parse_qs(urlparse(to_idp.headers["location"]).query).items()}
    assert sign_in["code_challenge_method"] == "S256" and sign_in["client_id"] == AUDIENCE
    fake.pkce["idp-code"] = sign_in["code_challenge"]
    back = http.get(f"/atlassian/signed-in?code=idp-code&state={sign_in['state']}")
    if back.status_code != 303:
        return back
    consent_url = urlparse(back.headers["location"])
    assert consent_url.netloc == "auth.atlassian.com"
    state = parse_qs(consent_url.query)["state"][0]
    code = "&code=atl-code" if consent else ""
    return http.get(f"/atlassian/callback?state={state}{code}")


def test_a_user_links_their_account_and_then_reads_as_themselves(
    http: TestClient, idp: Idp, fake: FakeAtlassian, store: Store
) -> None:
    with http:
        alice = Session(http, idp.token())
        assert alice.call("server_status")["structuredContent"]["atlassian"] == "not linked"
        refused = alice.call("jira_get_issue", key="OPS-12")
        assert "not linked" in text(refused)

        done = browse(http, fake, link_url(refused))
        assert done.status_code == 200 and "Alice Example" in done.text
        assert done.headers["cache-control"] == "no-store"

        status = alice.call("server_status")["structuredContent"]["atlassian"]
        assert status == "linked as Alice Example on https://acme.atlassian.net"
        issue = alice.call("jira_get_issue", key="OPS-12")["structuredContent"]
        assert issue == {
            "key": "OPS-12",
            "summary": "Rotate the signing key",
            "status": "In Progress",
            "assignee": "Alice Example",
            "updated": "2026-10-02T10:00:00.000+0000",
            "url": "https://acme.atlassian.net/browse/OPS-12",
            "type": "Task",
            "priority": "High",
            "reporter": "Bob Example",
            "created": "2026-10-01T10:00:00.000+0000",
            "description": {"text": "Rotate it.\nThen tell ops.", "truncated": False},
        }
        # The call to Atlassian carried Atlassian's token for alice, never her MCP token.
        sent = fake.requests[-1].headers["authorization"]
        assert sent == "Bearer at-1" and idp.token() not in sent

        found = alice.call("jira_search", jql="project = OPS", limit=5)["structuredContent"]
        assert [i["key"] for i in found["issues"]] == ["OPS-12"]
        assert dict(fake.requests[-1].url.params)["maxResults"] == "5"
        pages = alice.call("confluence_search", cql='text ~ "rotate"')["structuredContent"]["pages"]
        assert pages == [
            {
                "id": "4411",
                "title": "Key rotation runbook",
                "type": "page",
                "excerpt": "How to rotate keys & tell ops",
                "url": "https://acme.atlassian.net/wiki/spaces/OPS/pages/4411",
            }
        ]
        page = alice.call("confluence_get_page", page_id="4411")["structuredContent"]
        assert (page["title"], page["space"], page["version"]) == ("Key rotation runbook", "OPS", 7)
        assert page["body"] == {"text": "Steps Rotate & verify.", "truncated": False}

        # Another user is not linked by this, and gets their own link.
        bob = Session(http, idp.token(sub="user-bob"))
        assert "not linked" in text(bob.call("jira_get_issue", key="OPS-12"))


def test_a_link_opened_by_someone_else_links_nothing(
    http: TestClient, idp: Idp, fake: FakeAtlassian, store: Store
) -> None:
    """An attacker sends their own link to a victim. The victim's browser signs in as the
    victim, which is not who the link was made for."""
    with http:
        attacker = Session(http, idp.token(sub="user-mallory"))
        start = link_url(attacker.call("jira_search", jql="project = OPS"))
        fake.subject = "user-alice"  # the victim opens it
        refused = browse(http, fake, start)
        assert refused.status_code == 403 and "different user" in refused.text
        assert not any("auth.atlassian.com/oauth/token" in str(r.url) for r in fake.requests)
        assert asyncio.run(store.grant("user-mallory")) is None
        # And the link is dead: it can't be retried.
        assert http.get(start.removeprefix("http://mcp.test")).status_code == 400


def test_links_cannot_be_forged_replayed_or_skipped_ahead(
    http: TestClient, idp: Idp, fake: FakeAtlassian, store: Store
) -> None:
    with http:
        alice = Session(http, idp.token())
        start = link_url(alice.call("jira_search", jql="project = OPS"))
        assert http.get("/atlassian/link?request=made-up").status_code == 400
        assert http.get("/atlassian/signed-in?code=x&state=made-up").status_code == 400
        assert http.get("/atlassian/callback?code=x&state=made-up").status_code == 400
        # Straight to Atlassian's callback without signing in: there is no such state yet.
        link = asyncio.run(store.link(start.split("request=")[1]))
        assert link is not None and link.atlassian_state is None
        # A sign-in code that doesn't match the PKCE challenge is not redeemed.
        to_idp = http.get(start.removeprefix("http://mcp.test"))
        state = parse_qs(urlparse(to_idp.headers["location"]).query)["state"][0]
        fake.pkce["idp-code"] = "someone-elses-challenge"
        assert http.get(f"/atlassian/signed-in?code=idp-code&state={state}").status_code == 400
        # The full flow works once, and its last step can't be replayed.
        fresh = link_url(alice.call("jira_search", jql="project = OPS"))
        assert browse(http, fake, fresh).status_code == 200
        assert http.get(fresh.removeprefix("http://mcp.test")).status_code == 400


def test_declining_at_atlassian_links_nothing(
    http: TestClient, idp: Idp, fake: FakeAtlassian, store: Store
) -> None:
    with http:
        alice = Session(http, idp.token())
        start = link_url(alice.call("jira_search", jql="project = OPS"))
        declined = browse(http, fake, start, consent=False)
        assert declined.status_code == 400 and "declined" in declined.text
        assert asyncio.run(store.grant("user-alice")) is None


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("jira_get_issue", {"key": "OPS-12/../../../myself"}),
        ("jira_get_issue", {"key": "ops-12"}),
        ("jira_get_issue", {"key": "OPS-12?expand=names"}),
        ("jira_search", {"jql": " "}),
        ("jira_search", {"jql": "project = OPS", "limit": 500}),
        ("jira_search", {"jql": "x" * 1001}),
        ("confluence_get_page", {"page_id": "4411/child"}),
        ("confluence_get_page", {"page_id": "../user"}),
        ("confluence_search", {"cql": ""}),
    ],
)
def test_bad_arguments_never_reach_atlassian(
    http: TestClient,
    idp: Idp,
    fake: FakeAtlassian,
    store: Store,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    asyncio.run(store.save_grant(grant()))
    with http:
        result = Session(http, idp.token()).call(tool, **arguments)
        assert result.get("isError") is True
        assert not any(r.url.host == "api.atlassian.com" for r in fake.requests)


def test_a_grant_atlassian_revoked_is_dropped_and_the_user_told(
    http: TestClient, idp: Idp, fake: FakeAtlassian, store: Store
) -> None:
    asyncio.run(store.save_grant(grant()))
    fake.api_status = 401
    with http:
        alice = Session(http, idp.token())
        result = alice.call("jira_get_issue", key="OPS-12")
        assert result.get("isError") is True and "no longer valid" in text(result)
        assert "at-secret-value" not in json.dumps(result)
        assert "open this link" in text(alice.call("jira_get_issue", key="OPS-12")).lower()


def test_what_the_user_may_not_see_is_an_error_not_a_leak(
    http: TestClient, idp: Idp, fake: FakeAtlassian, store: Store
) -> None:
    asyncio.run(store.save_grant(grant()))
    fake.api_status = 404
    with http:
        result = Session(http, idp.token()).call("jira_get_issue", key="SEC-1")
        assert result.get("isError") is True and "no access" in text(result)


def test_without_atlassian_configured_there_are_no_atlassian_tools(idp: Idp) -> None:
    config = Settings(  # type: ignore[call-arg]
        _env_file=None,
        allowed_hosts="mcp.test",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        resource_url=RESOURCE,
    )
    verifier = JwtVerifier(ISSUER, AUDIENCE, RESOURCE, fetch=idp.fetch)
    http = TestClient(build_app(build_server(config, verifier), config), base_url="http://mcp.test")
    with http:
        session = Session(http, idp.token())
        assert session.call("server_status")["structuredContent"]["atlassian"] == "not configured"
        assert session.call("jira_search", jql="x").get("isError") is True
        assert http.get("/atlassian/link?request=x", headers=ACCEPT).status_code == 404
