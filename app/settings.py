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
    # Without an identity provider the server refuses to start, unless this is set: then
    # anyone who can reach the port can call it. For local development only.
    allow_anonymous: bool = False

    @property
    def auth_configured(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_audience)

    def hosts(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


settings = Settings()
