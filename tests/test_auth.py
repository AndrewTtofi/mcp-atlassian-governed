"""The server as an OAuth resource server (decision record 0002): which tokens it
accepts, and what it tells a client that has none."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from app.server import build_app, build_server
from app.settings import Settings
from app.tokens import REFETCH_SECONDS, JwtVerifier

ISSUER = "https://idp.test/application/o/mcp-atlassian/"
AUDIENCE = "mcp-atlassian"
RESOURCE = "http://mcp.test/mcp"
READ = "atlassian:read"


class Idp:
    """A stand-in identity provider: signing keys, a JWKS endpoint, and tokens."""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.fetches: list[str] = []
        self.down = False
        self.rotate("k1")

    def rotate(self, kid: str) -> None:
        self.keys = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048)}

    @property
    def kid(self) -> str:
        return next(iter(self.keys))

    async def fetch(self, url: str) -> dict[str, Any]:
        self.fetches.append(url)
        if self.down:
            raise httpx2.ConnectError("idp down")
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISSUER, "jwks_uri": f"{ISSUER}jwks/"}
        keys = []
        for kid, key in self.keys.items():
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": keys}

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user-alice",
            "iat": now,
            "exp": now + 300,
            "scope": f"openid {READ}",
            "preferred_username": "alice",
        }
        payload.update(claims)
        headers = {"kid": payload.pop("kid", self.kid)}
        key = payload.pop("signing_key", self.keys[self.kid])
        alg = payload.pop("alg", "RS256")
        return jwt.encode({k: v for k, v in payload.items() if v is not None}, key, alg, headers)


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def idp() -> Idp:
    return Idp()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier(idp: Idp, clock: Clock) -> JwtVerifier:
    return JwtVerifier(ISSUER, AUDIENCE, RESOURCE, fetch=idp.fetch, clock=clock)


# --- which tokens are accepted ---------------------------------------------------------


async def test_a_token_from_the_provider_for_this_server_is_accepted(
    verifier: JwtVerifier, idp: Idp
) -> None:
    token = idp.token()
    access = await verifier.verify_token(token)
    assert access is not None
    assert (access.subject, access.scopes) == ("user-alice", ["openid", READ])
    assert access.resource == RESOURCE  # what the SDK compares with this server's URL
    assert access.claims == {"iss": ISSUER, "name": "alice"}
    assert access.expires_at is not None and access.expires_at > time.time()


def forged(idp: Idp, how: str) -> str:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    match how:
        case "for another audience":
            return idp.token(aud="some-other-app")
        case "from another issuer":
            return idp.token(iss="https://evil.test/")
        case "expired":
            return idp.token(iat=now - 900, exp=now - 600)
        case "signed with another key":
            return idp.token(signing_key=other)
        case "no subject":
            return idp.token(sub=None)
        case "no expiry":
            return idp.token(exp=None)
        case "no audience":
            return idp.token(aud=None)
        case "symmetric algorithm":
            return idp.token(alg="HS256", signing_key="a-shared-secret-of-at-least-32-bytes!")
        case "unsigned":
            return idp.token(alg="none", signing_key=None)
        case "no key id":
            body = {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": now + 300}
            return jwt.encode(body, idp.keys[idp.kid], "RS256")
        case _:
            return "not.a.jwt"


@pytest.mark.parametrize(
    "how",
    [
        "for another audience",
        "from another issuer",
        "expired",
        "signed with another key",
        "no subject",
        "no expiry",
        "no audience",
        "symmetric algorithm",
        "unsigned",
        "no key id",
        "garbage",
    ],
)
async def test_every_other_token_is_refused(verifier: JwtVerifier, idp: Idp, how: str) -> None:
    assert await verifier.verify_token(idp.token()) is not None  # the real one works
    assert await verifier.verify_token(forged(idp, how)) is None


async def test_a_token_without_a_scope_claim_has_no_scopes(verifier: JwtVerifier, idp: Idp) -> None:
    access = await verifier.verify_token(idp.token(scope=None))
    assert access is not None and access.scopes == []


async def test_keys_are_fetched_once_and_found_through_discovery(
    verifier: JwtVerifier, idp: Idp
) -> None:
    for _ in range(3):
        assert await verifier.verify_token(idp.token()) is not None
    assert idp.fetches == [f"{ISSUER}.well-known/openid-configuration", f"{ISSUER}jwks/"]


async def test_a_rotated_key_is_picked_up_but_unknown_key_ids_cannot_hammer_the_provider(
    verifier: JwtVerifier, idp: Idp, clock: Clock
) -> None:
    assert await verifier.verify_token(idp.token()) is not None
    before = len(idp.fetches)
    for i in range(20):  # an attacker inventing key ids
        assert await verifier.verify_token(idp.token(kid=f"made-up-{i}")) is None
    assert len(idp.fetches) == before  # within the minute: no refetch at all
    idp.rotate("k2")
    assert await verifier.verify_token(idp.token()) is None  # not known yet
    clock.now = REFETCH_SECONDS + 1
    assert await verifier.verify_token(idp.token()) is not None
    assert len(idp.fetches) == before + 1


async def test_a_provider_outage_keeps_known_keys_working(
    verifier: JwtVerifier, idp: Idp, clock: Clock
) -> None:
    token = idp.token()
    assert await verifier.verify_token(token) is not None
    idp.down = True
    clock.now = REFETCH_SECONDS * 10
    assert await verifier.verify_token(token) is not None  # the key is cached
    assert await verifier.verify_token(idp.token(kid="new")) is None  # can't be checked


async def test_with_the_provider_down_from_the_start_nothing_is_accepted(
    idp: Idp, clock: Clock
) -> None:
    idp.down = True
    verifier = JwtVerifier(ISSUER, AUDIENCE, RESOURCE, fetch=idp.fetch, clock=clock)
    assert await verifier.verify_token(idp.token()) is None


# --- over HTTP -------------------------------------------------------------------------

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


@pytest.fixture
def http(idp: Idp, clock: Clock) -> TestClient:
    config = Settings(  # type: ignore[call-arg]
        _env_file=None,
        allowed_hosts="mcp.test",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        resource_url=RESOURCE,
    )
    verifier = JwtVerifier(ISSUER, AUDIENCE, RESOURCE, fetch=idp.fetch, clock=clock)
    return TestClient(build_app(build_server(config, verifier), config), base_url="http://mcp.test")


def bearer(token: str) -> dict[str, str]:
    return {**ACCEPT, "authorization": f"Bearer {token}"}


def result(resp: httpx2.Response) -> dict[str, Any]:
    """The JSON-RPC result from a JSON or an event-stream response."""
    if resp.headers["content-type"].startswith("application/json"):
        return dict(resp.json()["result"])
    data = next(line[6:] for line in resp.text.splitlines() if line.startswith("data: "))
    return dict(json.loads(data)["result"])


def test_without_a_token_the_client_is_told_where_to_sign_in(http: TestClient) -> None:
    with http:
        resp = http.post("/mcp", json=INITIALIZE, headers=ACCEPT)
        assert resp.status_code == 401
        challenge = resp.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert 'resource_metadata="http://mcp.test/.well-known/oauth-protected-resource/mcp"' in (
            challenge
        )


def test_the_resource_metadata_names_this_server_and_its_authorization_server(
    http: TestClient,
) -> None:
    with http:
        meta = http.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == RESOURCE
        assert meta["authorization_servers"] == [ISSUER]
        assert meta["scopes_supported"] == [READ]
        assert meta["bearer_methods_supported"] == ["header"]


def test_a_valid_token_reaches_the_tools_as_that_user(http: TestClient, idp: Idp) -> None:
    with http:
        headers = bearer(idp.token())
        opened = http.post("/mcp", json=INITIALIZE, headers=headers)
        assert opened.status_code == 200
        headers["mcp-session-id"] = opened.headers["mcp-session-id"]
        http.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
        )
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "server_status", "arguments": {}},
        }
        status = result(http.post("/mcp", json=call, headers=headers))["structuredContent"]
        assert status["signed_in_as"] == "user-alice"
        assert status["scopes"] == [READ, "openid"]
        assert idp.token() not in json.dumps(status)  # never the token itself


@pytest.mark.parametrize("how", ["for another audience", "expired", "symmetric algorithm"])
def test_a_token_that_is_not_for_this_server_is_a_401(http: TestClient, idp: Idp, how: str) -> None:
    with http:
        resp = http.post("/mcp", json=INITIALIZE, headers=bearer(forged(idp, how)))
        assert resp.status_code == 401
        # Which check failed is in the server's log, not in the answer.
        assert "audience" not in resp.text.lower() and "expired" not in resp.text.lower()


def test_a_token_without_the_scope_is_a_403_that_names_it(http: TestClient, idp: Idp) -> None:
    with http:
        resp = http.post("/mcp", json=INITIALIZE, headers=bearer(idp.token(scope="openid")))
        assert resp.status_code == 403
        challenge = resp.headers["www-authenticate"]
        assert 'error="insufficient_scope"' in challenge and READ in challenge
        assert "resource_metadata=" in challenge


def test_a_token_in_the_query_string_is_not_accepted(http: TestClient, idp: Idp) -> None:
    with http:
        resp = http.post(f"/mcp?access_token={idp.token()}", json=INITIALIZE, headers=ACCEPT)
        assert resp.status_code == 401


def test_health_and_metadata_need_no_token(http: TestClient) -> None:
    with http:
        assert http.get("/healthz").status_code == 200
        assert http.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
