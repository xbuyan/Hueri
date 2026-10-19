"""ASGI security gate placed in front of the MCP protocol handler.

Order of checks (cheapest and least informative first):
  1. Host header must be in the allowlist                  -> 421
  2. Origin header, if present, must be in the allowlist   -> 403
  3. No credentials in the query string                    -> 401
  4. Exactly one valid "Authorization: Bearer <token>"     -> 401
  5. Method must be POST (stateless JSON service)          -> 405

Nothing below the gate (the MCP SDK and its tools) runs unless all four pass.
Standard library only. Never logs header values, query strings or tokens.
401 responses deliberately carry no WWW-Authenticate header.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Dict, List
from urllib.parse import parse_qsl

from .settings import McpSettings

log = logging.getLogger("hueri.mcp.security")

_FORBIDDEN_QUERY_KEYS = frozenset({"token", "access_token"})
_MAX_CREDENTIAL_LENGTH = 512


def _headers(scope) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        out.setdefault(name, []).append(raw_value.decode("latin-1"))
    return out


class SecurityGate:
    def __init__(self, app, settings: McpSettings):
        self._app = app
        self._token_digest = hashlib.sha256(settings.bearer_token.encode("utf-8")).digest()
        self._hosts = frozenset(settings.allowed_hosts)
        self._origins = frozenset(settings.allowed_origins)

    async def __call__(self, scope, receive, send):
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope_type != "http":
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        headers = _headers(scope)

        # 1. Host
        hosts = headers.get("host", [])
        if len(hosts) != 1 or hosts[0].strip().lower() not in self._hosts:
            await self._reject(send, 421, "invalid_host")
            return

        # 2. Origin (absent is fine: non-browser clients send none)
        origins = headers.get("origin")
        if origins is not None:
            if len(origins) != 1 or origins[0].strip().lower().rstrip("/") not in self._origins:
                await self._reject(send, 403, "invalid_origin")
                return

        # 3. Credentials must never travel in the query string
        query = scope.get("query_string", b"").decode("latin-1")
        keys = {k.lower() for k, _ in parse_qsl(query, keep_blank_values=True)}
        if keys & _FORBIDDEN_QUERY_KEYS:
            await self._reject(send, 401, "credential_in_query")
            return

        # 4. Bearer token
        if not self._authorized(headers.get("authorization", [])):
            await self._reject(send, 401, "unauthorized")
            return

        # 5. Stateless JSON service: only POST is meaningful. GET would open a long-lived SSE
        #    stream and DELETE targets sessions; the MCP spec allows 405 for both.
        if scope.get("method") != "POST":
            await self._reject(send, 405, "method_not_allowed", [(b"allow", b"POST")])
            return

        await self._app(scope, receive, send)

    def _authorized(self, values: List[str]) -> bool:
        if len(values) != 1:
            return False
        scheme, sep, credential = values[0].partition(" ")
        if sep != " " or scheme.lower() != "bearer":
            return False
        if not credential or len(credential) > _MAX_CREDENTIAL_LENGTH:
            return False
        if any(ch.isspace() for ch in credential):
            return False
        digest = hashlib.sha256(credential.encode("utf-8", "ignore")).digest()
        return hmac.compare_digest(digest, self._token_digest)

    @staticmethod
    async def _reject(send, status: int, reason: str, extra_headers=()) -> None:
        log.warning("mcp request rejected: %s", reason)
        body = json.dumps({"error": reason}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    *extra_headers,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
