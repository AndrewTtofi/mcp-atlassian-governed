"""Access tokens: this server is an OAuth resource server (decision record 0002).

A token is accepted only if the configured identity provider signed it, for this server,
and it has not expired. Checks: signature (asymmetric algorithms only), issuer, audience,
expiry. Keys come from the provider's JWKS, cached by key id. An unknown key id triggers
one refetch (the provider rotated its keys), at most once a minute, so random key ids
can't be used to hammer the provider.

The token is used for one thing: knowing who is calling this server. It is never sent
anywhere else.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import jwt
from mcp.server.auth.provider import AccessToken

log = logging.getLogger(__name__)

# Asymmetric only. With a symmetric algorithm (HS256) the "public" key would be the
# signing key; PyJWT also refuses `none` when a list is given.
ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")
REFETCH_SECONDS = 60.0
LEEWAY_SECONDS = 30

Fetch = Callable[[str], Awaitable[dict[str, Any]]]


async def _http_get_json(url: str) -> dict[str, Any]:
    async with httpx2.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        resp = await client.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()
        body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("not a JSON object")
    return body


class JwtVerifier:
    """The SDK's `TokenVerifier`: a bearer token → who it is for, or None (a 401)."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        resource: str,
        jwks_url: str = "",
        fetch: Fetch = _http_get_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer, self.audience, self.resource = issuer, audience, resource
        self._jwks_url = jwks_url  # empty: read from the provider's discovery document
        self._fetch, self._now = fetch, clock
        self._keys: dict[str, Any] = {}  # kid → public key
        self._fetched_at: float | None = None

    async def _load_keys(self) -> None:
        if not self._jwks_url:
            discovery = f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
            self._jwks_url = str((await self._fetch(discovery))["jwks_uri"])
        jwks = jwt.PyJWKSet.from_dict(await self._fetch(self._jwks_url))
        self._keys = {k.key_id: k.key for k in jwks.keys if k.key_id}

    async def _key(self, kid: str) -> Any | None:
        stale = self._fetched_at is None or self._now() - self._fetched_at >= REFETCH_SECONDS
        if kid not in self._keys and stale:
            try:
                await self._load_keys()
            except Exception as exc:
                # The provider can't be reached. Tokens signed with keys already cached
                # keep working; anything else is refused until it is back.
                log.error(
                    "the identity provider's keys could not be fetched: %s", type(exc).__name__
                )
            finally:
                self._fetched_at = self._now()  # also after a failure: no retry storm
        return self._keys.get(kid)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return self._refuse("not a JWT")
        kid = header.get("kid")
        if header.get("alg") not in ALGORITHMS or not isinstance(kid, str):
            return self._refuse("algorithm or key id not acceptable")
        key = await self._key(kid)
        if key is None:
            return self._refuse("signed with a key the identity provider doesn't list")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=list(ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            # The class says which check failed (expired, audience, issuer, signature).
            return self._refuse(type(exc).__name__)
        scope = claims.get("scope")
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or self.audience),
            scopes=scope.split() if isinstance(scope, str) else [],
            expires_at=int(claims["exp"]),
            # The audience matched, so the token was issued for this server: report this
            # server's own resource URL, which is what the SDK compares against.
            resource=self.resource,
            subject=str(claims["sub"]),
            claims={
                "iss": claims["iss"],
                "name": claims.get("preferred_username") or claims.get("name"),
            },
        )

    @staticmethod
    def _refuse(why: str) -> None:
        # The reason goes to the log, never to the caller: a 401 that explains which
        # check failed helps someone forging tokens more than it helps a real client.
        log.warning("access token refused: %s", why)
        return None
