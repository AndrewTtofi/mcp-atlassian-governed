"""Asking the gateway whether a user may use a tool (decision record 0006).

This server knows who is calling and holds their Atlassian grant. Whether they *should*
be using a tool, with their role and the class of data involved, is the organisation's
policy, and that lives in one place: the governed AI gateway. So before a tool runs, the
server asks it, with its own credential.

The user's token is not sent. The gateway gets the token's subject and looks the user up
in its own directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx2

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str  # the gateway's code (ok, role_not_allowed, …) or gateway_unavailable
    reason: str


UNAVAILABLE = Decision(False, "gateway_unavailable", "the policy service could not be reached")


class GatewayPolicy:
    def __init__(
        self,
        url: str,
        key: str,
        connector: str,
        data_class: str,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{url.rstrip('/')}/v1/authorize"
        self._key, self.connector, self.data_class = key, connector, data_class
        self._transport = transport

    async def decide(self, subject: str, tool: str) -> Decision:
        """Anything but a clear yes is a no: an outage, an error or an answer this code
        doesn't understand all refuse the call."""
        question = {
            "subject": subject,
            "connector": self.connector,
            "tool": tool,
            "data_class": self.data_class,
        }
        try:
            async with httpx2.AsyncClient(timeout=5.0, transport=self._transport) as client:
                resp = await client.post(
                    self._url, json=question, headers={"authorization": f"Bearer {self._key}"}
                )
        except httpx2.HTTPError as exc:
            log.error("gateway unreachable: %s", type(exc).__name__)
            return UNAVAILABLE
        if resp.status_code != 200:
            # 403 here means this server's own key may not ask: a deployment fault.
            log.error("gateway refused the question: HTTP %d", resp.status_code)
            return UNAVAILABLE
        try:
            answer = resp.json()
            allowed, code = answer["allowed"], str(answer["code"])
        except (ValueError, KeyError, TypeError):
            log.error("gateway sent an answer that could not be read")
            return UNAVAILABLE
        if not isinstance(allowed, bool):
            log.error("gateway sent an answer that could not be read")
            return UNAVAILABLE
        if not allowed:
            return Decision(False, code[:64], str(answer.get("reason", ""))[:200])
        return Decision(True, "ok", "ok")
