"""An audit record for every tool call, and a rate limit per user (decision record 0005).

A record says who called which tool, what kind of tool it is, and how it ended. It never
holds the arguments or the result: it can show that a comment was deleted and by whom,
not what the comment said. Each record is a JSON log line (logger `mcp.audit`) and a row
in the `audit` table.

Both sit in the MCP layer as middleware, so they see every `tools/call` whatever happens
next: an unknown tool, a refused argument, a question to the user, a failure at Atlassian.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import CallToolResult, TextContent

from app.store import Store

log = logging.getLogger("mcp.audit")
WINDOW_SECONDS = 60.0


class RateLimiter:
    """Calls per user in the last minute (a sliding window, in this process). It stops a
    looping agent from spending a user's Atlassian rate limit or flooding a project with
    comments; it is not a security boundary."""

    MAX_USERS = 10_000

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute, self._now = per_minute, clock
        self._calls: dict[str, list[float]] = {}

    def allow(self, subject: str) -> bool:
        now = self._now()
        recent = [t for t in self._calls.get(subject, []) if now - t < WINDOW_SECONDS]
        if len(recent) >= self.per_minute:
            self._calls[subject] = recent
            return False
        if subject not in self._calls and len(self._calls) >= self.MAX_USERS:
            self._calls.clear()  # bounded memory; at worst everyone gets a fresh minute
        recent.append(now)
        self._calls[subject] = recent
        return True


def refused(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


def emit(record: dict[str, Any]) -> None:
    log.info(json.dumps(record, separators=(",", ":")))


class Audit:
    """SDK middleware: `(ctx, call_next) -> result`."""

    def __init__(
        self,
        store: Store | None,
        kinds: dict[str, str],
        calls: RateLimiter,
        writes: RateLimiter,
        who: Callable[[], Any] = get_access_token,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store, self.kinds, self.calls, self.writes = store, kinds, calls, writes
        self._who, self._now = who, clock

    async def record(
        self,
        subject: str | None,
        event: str,
        kind: str,
        outcome: str,
        detail: str | None = None,
        ms: int | None = None,
    ) -> None:
        emit(
            {
                "subject": subject or "anonymous",
                "event": event[:80],
                "kind": kind,
                "outcome": outcome,
                "detail": detail,
                "ms": ms,
            }
        )
        if self.store is not None:
            try:
                await self.store.audit(subject, event, kind, outcome, detail, ms)
            except Exception as exc:  # the log line above is the record of last resort
                log.error("audit row not written: %s", type(exc).__name__)

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params if isinstance(ctx.params, dict) else {}
        name = params.get("name")
        # The name is client input: only a known tool's name is recorded as given.
        tool = name if isinstance(name, str) and name in self.kinds else "unknown"
        kind = self.kinds.get(tool, "read")
        caller = self._who()
        subject = caller.subject if caller is not None else None
        key = subject or "anonymous"
        if not self.calls.allow(key) or (kind != "read" and not self.writes.allow(key)):
            await self.record(subject, f"tool:{tool}", kind, "rate_limited")
            return refused("Too many calls in the last minute. Wait a little and try again.")
        started = self._now()
        try:
            result = await call_next(ctx)
        except Exception as exc:
            await self.record(
                subject, f"tool:{tool}", kind, "error", type(exc).__name__, self._ms(started)
            )
            raise
        await self.record(subject, f"tool:{tool}", kind, *self._outcome(result), self._ms(started))
        return result

    def _ms(self, started: float) -> int:
        return int((self._now() - started) * 1000)

    @staticmethod
    def _outcome(result: Any) -> tuple[str, str | None]:
        def field(snake: str, camel: str) -> Any:
            if isinstance(result, dict):
                return result.get(camel, result.get(snake))
            return getattr(result, snake, None)

        if field("result_type", "resultType") == "input_required":
            return "asked", None  # the person is being asked to confirm; a second call follows
        if field("is_error", "isError"):
            return "error", None
        structured = field("structured_content", "structuredContent")
        if isinstance(structured, dict) and structured.get("deleted") is False:
            return "not_confirmed", None
        return "ok", None
