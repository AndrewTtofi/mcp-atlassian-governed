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

    def hosts(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


settings = Settings()
