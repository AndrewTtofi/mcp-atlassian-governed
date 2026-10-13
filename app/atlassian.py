"""Atlassian Cloud: the OAuth 2.0 (3LO) grant each user gives this server, and the calls
made with it (decision record 0003).

Every API call carries one user's own access token, so Atlassian applies that user's
permissions: the server cannot return what the user could not open themselves.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx2

from app.store import Grant, Store

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"  # noqa: S105 (an endpoint, not a secret)
API = "https://api.atlassian.com"
REFRESH_BEFORE_SECONDS = 60  # an access token this close to expiry is replaced first


class AtlassianError(Exception):
    """Atlassian failed or refused. The message is safe to show the caller."""


class NotLinked(AtlassianError):
    """This user has no usable grant: they have to link their account (again)."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: str


class Atlassian:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_url: str,
        scopes: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id, self._secret = client_id, client_secret
        self.redirect_url, self.scopes = redirect_url, scopes
        self._transport, self._now = transport, clock

    def _client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(timeout=15.0, follow_redirects=False, transport=self._transport)

    # --- the grant ---------------------------------------------------------------------

    def authorize_url(self, state: str) -> str:
        """Where the user's browser goes to consent. `prompt=consent` so they always see
        what they are granting; `offline_access` (in the scopes) is what yields a refresh
        token."""
        query = {
            "audience": "api.atlassian.com",
            "client_id": self.client_id,
            "scope": self.scopes,
            "redirect_uri": self.redirect_url,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def _token(self, body: dict[str, str]) -> TokenSet:
        payload = {"client_id": self.client_id, "client_secret": self._secret, **body}
        try:
            async with self._client() as client:
                resp = await client.post(TOKEN_URL, json=payload)
        except httpx2.HTTPError as exc:
            log.warning("atlassian token endpoint unreachable: %s", type(exc).__name__)
            raise AtlassianError("Atlassian could not be reached") from None
        if resp.status_code in (400, 401, 403):
            # invalid_grant: the refresh token was used, revoked or expired, or the user
            # changed their password. Only a new consent fixes it.
            raise NotLinked("your Atlassian link is no longer valid")
        if resp.status_code != 200:
            raise AtlassianError(f"Atlassian's token endpoint answered HTTP {resp.status_code}")
        try:
            data = resp.json()
            return TokenSet(
                access_token=str(data["access_token"]),
                # Rotating refresh tokens: every exchange returns a new one and disables
                # the one that was used.
                refresh_token=str(data["refresh_token"]),
                expires_at=self._now() + float(data["expires_in"]),
                scopes=str(data.get("scope", "")),
            )
        except (ValueError, KeyError, TypeError):
            raise AtlassianError("Atlassian sent an unexpected token response") from None

    async def exchange(self, code: str) -> TokenSet:
        return await self._token(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_url}
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    # --- calls as the user -------------------------------------------------------------

    async def _get(self, url: str, access_token: str, params: dict[str, Any] | None = None) -> Any:
        return await self._send("GET", url, access_token, params=params)

    async def _send(
        self,
        method: str,
        url: str,
        access_token: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            async with self._client() as client:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers={
                        "authorization": f"Bearer {access_token}",
                        "accept": "application/json",
                    },
                )
        except httpx2.HTTPError as exc:
            log.warning("atlassian request failed: %s", type(exc).__name__)
            raise AtlassianError("Atlassian could not be reached") from None
        if resp.status_code == 401:
            raise NotLinked("your Atlassian link is no longer valid")
        if resp.status_code in (403, 404):
            # The same answer for both, as Atlassian itself gives for things you may not see.
            raise AtlassianError("not found, or your Atlassian account has no access to it")
        if resp.status_code == 400:
            raise AtlassianError("Atlassian rejected the query")
        if resp.status_code == 429:
            raise AtlassianError("Atlassian is rate limiting this account; try again shortly")
        if resp.status_code == 204:  # done, nothing to return (a delete)
            return None
        if resp.status_code not in (200, 201):
            raise AtlassianError(f"Atlassian answered HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise AtlassianError("Atlassian sent an unexpected response") from None

    async def sites(self, access_token: str) -> list[dict[str, Any]]:
        """The sites this grant can reach: [{id (the cloud id), url, name, scopes}]."""
        found = await self._get(f"{API}/oauth/token/accessible-resources", access_token)
        return [s for s in found if isinstance(s, dict)] if isinstance(found, list) else []

    async def me(self, access_token: str) -> str:
        who = await self._get(f"{API}/me", access_token)
        return str(who.get("name") or who.get("email") or "") if isinstance(who, dict) else ""

    async def jira(self, grant: Grant, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._get(f"{API}/ex/jira/{grant.cloud_id}{path}", grant.access_token, params)

    async def jira_write(
        self, grant: Grant, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        """A change in Jira, made as the user: Jira records them as its author."""
        return await self._send(
            method, f"{API}/ex/jira/{grant.cloud_id}{path}", grant.access_token, body=body
        )

    async def confluence(
        self, grant: Grant, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        return await self._get(
            f"{API}/ex/confluence/{grant.cloud_id}{path}", grant.access_token, params
        )


class Grants:
    """A user's grant, ready to use: refreshed when it is about to expire."""

    def __init__(self, store: Store, atlassian: Atlassian, clock: Callable[[], float] = time.time):
        self.store, self.atlassian, self._now = store, atlassian, clock
        self._locks: dict[str, asyncio.Lock] = {}

    async def current(self, subject: str) -> Grant:
        grant = await self.store.grant(subject)
        if grant is None:
            raise NotLinked("your Atlassian account is not linked")
        if grant.expires_at - self._now() > REFRESH_BEFORE_SECONDS:
            return grant
        # One refresh per user at a time. A refresh token works once: two concurrent
        # refreshes would leave one caller holding a token Atlassian has disabled.
        async with self._locks.setdefault(subject, asyncio.Lock()):
            grant = await self.store.grant(subject)  # another call may have refreshed it
            if grant is None:
                raise NotLinked("your Atlassian account is not linked")
            if grant.expires_at - self._now() > REFRESH_BEFORE_SECONDS:
                return grant
            try:
                tokens = await self.atlassian.refresh(grant.refresh_token)
            except NotLinked:
                await self.store.delete_grant(subject)  # dead: don't try it again
                raise
            fresh = Grant(
                subject=grant.subject,
                cloud_id=grant.cloud_id,
                site_url=grant.site_url,
                account=grant.account,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                expires_at=tokens.expires_at,
                scopes=tokens.scopes or grant.scopes,
            )
            # Stored before it is used: if the process dies now, the new refresh token is
            # not lost with it.
            await self.store.save_grant(fresh)
            return fresh

    async def forget(self, subject: str) -> bool:
        """Delete a user's grant. True if there was one. Under the same lock as a refresh:
        a refresh in flight would otherwise store its new pair after the delete, and the
        user would still be linked."""
        async with self._locks.setdefault(subject, asyncio.Lock()):
            had = await self.store.grant(subject) is not None
            await self.store.delete_grant(subject)
            return had
