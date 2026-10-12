"""Step-up for tools that change things (decision record 0004).

Reading needs the read scope, which the SDK checks on every request. A tool that writes
also needs the write scope. The MCP authorization specification asks for that to be said
in HTTP: a 403 with `WWW-Authenticate: Bearer error="insufficient_scope", scope="…"`, so
a client can take the user through consent for exactly what is missing and retry.

The tool name is inside the JSON-RPC body, so this looks at the body before the MCP layer
does, and passes it on untouched.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.tokens import JwtVerifier

MAX_BODY = 4 * 1024 * 1024  # the SDK's own limit for a request body


def called_tools(body: bytes) -> set[str]:
    """The tools a JSON-RPC body asks to call (one message or a batch)."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return set()
    names = set()
    for message in parsed if isinstance(parsed, list) else [parsed]:
        if isinstance(message, dict) and message.get("method") == "tools/call":
            params = message.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if isinstance(name, str):
                names.add(name)
    return names


class WriteScopeGate:
    def __init__(
        self,
        app: ASGIApp,
        verifier: JwtVerifier,
        write_tools: frozenset[str],
        read_scope: str,
        write_scope: str,
        resource_metadata_url: str,
        path: str = "/mcp",
        audit: Any = None,
    ) -> None:
        self._audit = audit
        self.app, self.verifier, self.write_tools = app, verifier, write_tools
        self.read_scope, self.write_scope = read_scope, write_scope
        self.resource_metadata_url, self.path = resource_metadata_url, path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return
        chunks: list[Message] = []
        body, more = b"", True
        while more and len(body) <= MAX_BODY:
            message = await receive()
            chunks.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)

        wanted = called_tools(body) & self.write_tools
        if wanted:
            subject, may_write = await self._caller(scope)
            if not may_write:
                if self._audit is not None:
                    for tool in sorted(wanted):
                        await self._audit.record(
                            subject, f"tool:{tool}", "write", "refused", "insufficient_scope"
                        )
                await self._challenge(send)
                return

        async def replay() -> Message:  # hand the body on exactly as it arrived
            return chunks.pop(0) if chunks else await receive()

        await self.app(scope, replay, send)

    async def _caller(self, scope: Scope) -> tuple[str | None, bool]:
        """(who is calling, whether they may write)."""
        headers = dict(scope["headers"])
        value = headers.get(b"authorization", b"").decode("latin-1")
        if not value.lower().startswith("bearer "):
            return None, True  # no token at all: the SDK answers 401, the right answer
        access = await self.verifier.verify_token(value[7:].strip())
        if access is None:
            return None, True  # not a valid token: 401, same reason
        return access.subject, self.write_scope in access.scopes

    async def _challenge(self, send: Send) -> None:
        # Both scopes: the client asks for the union, so the new token still reads.
        challenge = (
            f'Bearer error="insufficient_scope", scope="{self.read_scope} {self.write_scope}", '
            f'resource_metadata="{self.resource_metadata_url}", '
            'error_description="This tool changes data and needs the write scope"'
        )
        body: bytes = json.dumps(
            {"error": "insufficient_scope", "error_description": "write scope required"}
        ).encode()
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", challenge.encode()),
        ]
        start: dict[str, Any] = {"type": "http.response.start", "status": 403, "headers": headers}
        await send(start)
        await send({"type": "http.response.body", "body": body})
