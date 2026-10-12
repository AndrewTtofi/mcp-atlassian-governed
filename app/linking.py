"""Linking a user's Atlassian account, in their browser (decision record 0003).

The danger in any "open this URL to connect your account" flow is that the person who
opens the URL is not the person it was made for: an attacker sends their own link to a
victim, the victim consents, and the victim's Atlassian account ends up attached to the
attacker's identity. So before the browser is sent to Atlassian, it has to prove who is
sitting at it:

    /atlassian/link?request=…     a one-time handle the MCP tool handed out
        → sign in at the identity provider (authorization code + PKCE)
    /atlassian/signed-in          the ID token's subject must be the user the handle
                                  was made for, or the flow stops here
        → consent at Atlassian
    /atlassian/callback           the grant is stored, encrypted, for that user
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx2
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.atlassian import Atlassian, AtlassianError
from app.settings import Settings
from app.store import Grant, Store
from app.tokens import JwtVerifier

log = logging.getLogger(__name__)


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """A small page for a person. Everything interpolated is escaped."""
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        "<body style='font:16px system-ui;max-width:34rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.3rem'>{html.escape(title)}</h1><p>{html.escape(body)}</p></body>",
        status_code=status,
        # Nothing here may be framed or cached, and no referrer carries the handle on.
        headers={
            "cache-control": "no-store",
            "referrer-policy": "no-referrer",
            "content-security-policy": (
                "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
            ),
        },
    )


EXPIRED = (
    "This link is not valid",
    "It has expired or was already used. Ask your assistant again to get a new one.",
)


class Linking:
    def __init__(
        self,
        config: Settings,
        store: Store,
        atlassian: Atlassian,
        verifier: JwtVerifier,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.config, self.store, self.atlassian, self.verifier = config, store, atlassian, verifier
        self._transport = transport
        self._endpoints: tuple[str, str] | None = None

    @property
    def _signed_in_url(self) -> str:
        return f"{self.config.public_url.rstrip('/')}/atlassian/signed-in"

    def url_for(self, link_id: str) -> str:
        return f"{self.config.public_url.rstrip('/')}/atlassian/link?request={link_id}"

    async def _idp(self) -> tuple[str, str]:
        """The identity provider's authorize and token endpoints."""
        if self._endpoints is None:
            authorize, token = self.config.oidc_authorize_url, self.config.oidc_token_url
            if not (authorize and token):
                url = f"{self.config.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
                async with httpx2.AsyncClient(timeout=5.0, transport=self._transport) as client:
                    meta = (await client.get(url)).json()
                authorize = authorize or str(meta["authorization_endpoint"])
                token = token or str(meta["token_endpoint"])
            self._endpoints = (authorize, token)
        return self._endpoints

    async def start(self, request: Request) -> Response:
        link = await self.store.link(request.query_params.get("request", ""))
        if link is None:
            return page(*EXPIRED, status=400)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        await self.store.start_sign_in(link.id, state, verifier)
        authorize, _ = await self._idp()
        query = {
            "client_id": self.config.oidc_audience,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": self._signed_in_url,
            "state": state,
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
        }
        return RedirectResponse(f"{authorize}?{urlencode(query)}", status_code=303)

    async def signed_in(self, request: Request) -> Response:
        link = await self.store.link_by_idp_state(request.query_params.get("state", ""))
        code = request.query_params.get("code", "")
        if link is None or not code or not link.pkce_verifier:
            return page(*EXPIRED, status=400)
        subject = await self._subject(code, link.pkce_verifier)
        if subject is None:
            return page("Sign-in failed", "The identity provider did not confirm who you are.", 400)
        if subject != link.subject:
            # The whole point of the detour: this browser belongs to someone else.
            log.warning("account link refused: opened by a different user than it was made for")
            await self.store.delete_link(link.id)
            return page(
                "This link was made for a different user",
                "You are signed in as someone else. Nothing was linked. If you did not ask "
                "for this link yourself, ignore it.",
                403,
            )
        state = secrets.token_urlsafe(32)
        await self.store.mark_verified(link.id, state)
        return RedirectResponse(self.atlassian.authorize_url(state), status_code=303)

    async def _subject(self, code: str, verifier: str) -> str | None:
        """Redeem the sign-in code → the subject of its ID token."""
        _, token_url = await self._idp()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._signed_in_url,
            "client_id": self.config.oidc_audience,
            "code_verifier": verifier,
        }
        try:
            async with httpx2.AsyncClient(timeout=10.0, transport=self._transport) as client:
                resp = await client.post(token_url, data=form)
            id_token = resp.json().get("id_token") if resp.status_code == 200 else None
        except (httpx2.HTTPError, ValueError) as exc:
            log.warning("sign-in code could not be redeemed: %s", type(exc).__name__)
            return None
        if not isinstance(id_token, str):
            return None
        claims: dict[str, Any] | None = await self.verifier.claims(id_token)
        return str(claims["sub"]) if claims else None

    async def callback(self, request: Request) -> Response:
        link = await self.store.link_by_atlassian_state(request.query_params.get("state", ""))
        if link is None or not link.verified:
            return page(*EXPIRED, status=400)
        await self.store.delete_link(link.id)  # one use, whatever happens next
        code = request.query_params.get("code", "")
        if not code:  # the user pressed Cancel at Atlassian
            return page("Nothing was linked", "You declined at Atlassian.", 400)
        try:
            tokens = await self.atlassian.exchange(code)
            sites = await self.atlassian.sites(tokens.access_token)
            if not sites:
                return page(
                    "Nothing was linked", "That account has no site this app can reach.", 400
                )
            site = sites[0]
            account = await self.atlassian.me(tokens.access_token)
        except AtlassianError as exc:
            return page("Linking failed", str(exc), 502)
        await self.store.save_grant(
            Grant(
                subject=link.subject,
                cloud_id=str(site.get("id", "")),
                site_url=str(site.get("url", "")),
                account=account,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                expires_at=tokens.expires_at,
                scopes=tokens.scopes,
            )
        )
        return page(
            "Atlassian account linked",
            f"{account or 'Your account'} on {site.get('url', 'your site')} is now linked. "
            "You can close this tab and return to your assistant.",
        )
