"""What the server has to remember: each user's Atlassian grant, and account links in
progress (decision record 0003).

One SQLite file. Tokens are encrypted before they are written (Fernet: AES with an
authentication tag), with a key that is only in the environment, so the file by itself is
not a credential. Several keys may be configured, the first one encrypts and all of them
decrypt, which is how a key is rotated without a re-link.

sqlite3 is blocking, so every call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

LINK_SECONDS = 600  # how long a user has to finish linking their account

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    subject     TEXT PRIMARY KEY,   -- the identity provider's `sub`
    cloud_id    TEXT NOT NULL,
    site_url    TEXT NOT NULL,
    account     TEXT NOT NULL,      -- the Atlassian account's display name, for humans
    access_enc  BLOB NOT NULL,
    refresh_enc BLOB NOT NULL,
    expires_at  REAL NOT NULL,      -- when the access token stops working (unix time)
    scopes      TEXT NOT NULL,
    linked_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    id              TEXT PRIMARY KEY,  -- the handle in the URL the user opens
    subject         TEXT NOT NULL,     -- who asked for it, through MCP
    created_at      REAL NOT NULL,
    idp_state       TEXT,              -- set when the browser is sent to sign in
    pkce_verifier   TEXT,
    verified        INTEGER NOT NULL DEFAULT 0,  -- the browser proved to be `subject`
    atlassian_state TEXT               -- set when the browser is sent to Atlassian
);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    subject TEXT NOT NULL,   -- the identity provider's `sub`, or "anonymous"
    event   TEXT NOT NULL,   -- "tool:<name>", "link", "scope"
    kind    TEXT NOT NULL,   -- read | write | destructive | account
    outcome TEXT NOT NULL,   -- ok | error | refused | not_confirmed | rate_limited | asked
    detail  TEXT,            -- a fixed word or an exception class, never content
    ms      INTEGER
);
CREATE INDEX IF NOT EXISTS audit_at ON audit (at);
CREATE INDEX IF NOT EXISTS audit_subject ON audit (subject, at);
"""


@dataclass(frozen=True)
class Grant:
    subject: str
    cloud_id: str
    site_url: str
    account: str
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: str


@dataclass(frozen=True)
class Link:
    id: str
    subject: str
    created_at: float
    idp_state: str | None
    pkce_verifier: str | None
    verified: bool
    atlassian_state: str | None


class StoreError(RuntimeError):
    """The store can't be used as configured."""


def new_key() -> str:
    """A key for TOKEN_ENCRYPTION_KEY."""
    return Fernet.generate_key().decode()


class Store:
    def __init__(
        self,
        path: str,
        keys: str,
        clock: Callable[[], float] = time.time,
        audit_retention_days: int = 0,
    ) -> None:
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        if not parts:
            raise StoreError("TOKEN_ENCRYPTION_KEY is not set")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in parts])
        except ValueError as exc:
            raise StoreError("TOKEN_ENCRYPTION_KEY is not a valid key") from exc
        self._path, self._now = path, clock
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection, used from worker threads one call at a time (the lock).
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        if audit_retention_days:
            self._db.execute(
                "DELETE FROM audit WHERE at < ?", (self._now() - audit_retention_days * 86400,)
            )
        self._lock = asyncio.Lock()

    async def _run(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(lambda: self._db.execute(sql, args).fetchall())

    def close(self) -> None:
        self._db.close()

    # --- grants ------------------------------------------------------------------------

    async def save_grant(self, grant: Grant) -> None:
        await self._run(
            "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(subject) DO UPDATE SET "
            "cloud_id=excluded.cloud_id, site_url=excluded.site_url, account=excluded.account, "
            "access_enc=excluded.access_enc, refresh_enc=excluded.refresh_enc, "
            "expires_at=excluded.expires_at, scopes=excluded.scopes",
            (
                grant.subject,
                grant.cloud_id,
                grant.site_url,
                grant.account,
                self._fernet.encrypt(grant.access_token.encode()),
                self._fernet.encrypt(grant.refresh_token.encode()),
                grant.expires_at,
                grant.scopes,
                self._now(),
            ),
        )

    async def grant(self, subject: str) -> Grant | None:
        rows = await self._run("SELECT * FROM grants WHERE subject = ?", (subject,))
        if not rows:
            return None
        row = rows[0]
        try:
            access = self._fernet.decrypt(row["access_enc"]).decode()
            refresh = self._fernet.decrypt(row["refresh_enc"]).decode()
        except InvalidToken:
            # Written with a key that is no longer configured: unusable. The user links again.
            await self.delete_grant(subject)
            return None
        return Grant(
            subject=row["subject"],
            cloud_id=row["cloud_id"],
            site_url=row["site_url"],
            account=row["account"],
            access_token=access,
            refresh_token=refresh,
            expires_at=row["expires_at"],
            scopes=row["scopes"],
        )

    async def delete_grant(self, subject: str) -> None:
        await self._run("DELETE FROM grants WHERE subject = ?", (subject,))

    # --- audit -------------------------------------------------------------------------

    async def audit(
        self,
        subject: str | None,
        event: str,
        kind: str,
        outcome: str,
        detail: str | None = None,
        ms: int | None = None,
    ) -> None:
        """One row per decision or call. Callers pass names and fixed words only: no
        arguments, no results, no tokens."""
        await self._run(
            "INSERT INTO audit (at, subject, event, kind, outcome, detail, ms) "
            "VALUES (?,?,?,?,?,?,?)",
            (self._now(), subject or "anonymous", event[:80], kind, outcome, detail, ms),
        )

    async def audit_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._run("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- links in progress -------------------------------------------------------------

    async def create_link(self, subject: str) -> str:
        """A one-time handle for the URL the user opens. Any earlier one of theirs is void."""
        link_id = secrets.token_urlsafe(32)
        await self._run(
            "DELETE FROM links WHERE subject = ? OR created_at < ?",
            (
                subject,
                self._now() - LINK_SECONDS,
            ),
        )
        await self._run(
            "INSERT INTO links (id, subject, created_at) VALUES (?,?,?)",
            (link_id, subject, self._now()),
        )
        return link_id

    async def _link(self, column: str, value: str) -> Link | None:
        rows = await self._run(
            f"SELECT * FROM links WHERE {column} = ? AND created_at >= ?",  # noqa: S608 (column is ours)
            (value, self._now() - LINK_SECONDS),
        )
        if not rows:
            return None
        r = rows[0]
        return Link(
            r["id"],
            r["subject"],
            r["created_at"],
            r["idp_state"],
            r["pkce_verifier"],
            bool(r["verified"]),
            r["atlassian_state"],
        )

    async def link(self, link_id: str) -> Link | None:
        return await self._link("id", link_id)

    async def link_by_idp_state(self, state: str) -> Link | None:
        return await self._link("idp_state", state)

    async def link_by_atlassian_state(self, state: str) -> Link | None:
        return await self._link("atlassian_state", state)

    async def start_sign_in(self, link_id: str, state: str, verifier: str) -> None:
        await self._run(
            "UPDATE links SET idp_state = ?, pkce_verifier = ? WHERE id = ?",
            (state, verifier, link_id),
        )

    async def mark_verified(self, link_id: str, atlassian_state: str) -> None:
        # The sign-in state is cleared: that step can't be replayed.
        await self._run(
            "UPDATE links SET verified = 1, idp_state = NULL, pkce_verifier = NULL, "
            "atlassian_state = ? WHERE id = ?",
            (atlassian_state, link_id),
        )

    async def delete_link(self, link_id: str) -> None:
        await self._run("DELETE FROM links WHERE id = ?", (link_id,))


if __name__ == "__main__":  # python -m app.store → a new TOKEN_ENCRYPTION_KEY
    print(new_key())
