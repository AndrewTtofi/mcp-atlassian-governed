"""Settings from the environment. `.env.example` documents every variable."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Where the server listens. In a container that is every interface; the published
    # port decides who can reach it.
    host: str = "127.0.0.1"
    port: int = 8080
    # The names clients may use to reach the server (the Host header), comma-separated.
    # Anything else is refused: a web page a user visits must not be able to drive a
    # server on their machine through a DNS name it controls (DNS rebinding).
    allowed_hosts: str = "localhost:8080,127.0.0.1:8080"
    log_level: str = "INFO"

    # Identity (decision record 0002). The server accepts access tokens the identity
    # provider issued for it: OIDC_ISSUER is exactly the tokens' `iss`, OIDC_AUDIENCE must
    # be in their `aud`. OIDC_JWKS_URL is optional (default: from the issuer's discovery
    # document).
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    # This server's own address as clients know it: its OAuth resource identifier.
    resource_url: str = "http://localhost:8080/mcp"
    # The scope every call needs.
    read_scope: str = "atlassian:read"
    # The scope a call that changes something also needs.
    write_scope: str = "atlassian:write"
    # The tools that change things, by name, comma-separated. A tool not listed here does
    # not exist as far as clients can tell. Empty: the server is read-only.
    write_tools: str = ""
    # Without an identity provider the server refuses to start, unless this is set: then
    # anyone who can reach the port can call it. For local development only.
    allow_anonymous: bool = False

    # Where a browser signs in, and where this server redeems the code. Defaults: from the
    # issuer's discovery document. Set them when users and this server reach the identity
    # provider at different addresses (a compose network).
    oidc_authorize_url: str = ""
    oidc_token_url: str = ""

    # This server's address as a user's browser reaches it.
    public_url: str = "http://localhost:8080"

    # Atlassian (decision record 0003): the OAuth 2.0 (3LO) app each user grants access to.
    atlassian_client_id: str = ""
    atlassian_client_secret: str = ""
    # What a user is asked to grant. `offline_access` is what yields a refresh token.
    atlassian_scopes: str = (
        "read:jira-work read:jira-user read:confluence-content.all search:confluence "
        "read:me offline_access"
    )
    # Encrypts stored Atlassian tokens. Comma-separated: the first key encrypts, all
    # decrypt (key rotation). Generate: python -m app.store
    token_encryption_key: str = ""
    database_path: str = "data/mcp.sqlite3"

    @property
    def auth_configured(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_audience)

    @property
    def atlassian_configured(self) -> bool:
        return bool(self.atlassian_client_id and self.atlassian_client_secret)

    @property
    def atlassian_redirect_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/atlassian/callback"

    def enabled_write_tools(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.write_tools.split(",") if t.strip())

    def hosts(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


settings = Settings()
