"""Minimal stateless MCP client (Streamable HTTP, JSON responses), standard library only.

Every tool call does its own initialize -> initialized -> tools/call round trip, matching
the server's stateless design. The bearer token is only ever placed in the Authorization
header and never appears in errors, traces or logs.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

PROTOCOL_VERSION = "2025-11-25"
_ACCEPT = "application/json, text/event-stream"


class McpError(Exception):
    """Raised for any failure talking to the MCP server. Never carries the token."""


def _parse_body(body: bytes, content_type: str) -> Dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in (content_type or ""):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise McpError("MCP server returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise McpError("MCP server returned an unexpected response shape")
    return data


def _unwrap_result(result: Dict[str, Any]) -> Dict[str, Any]:
    if result.get("isError"):
        raise McpError("MCP tool reported an error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                parsed = json.loads(block.get("text", ""))
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise McpError("MCP tool result had no usable content")


class McpClient:
    def __init__(self, url: str, token: str, *, timeout: float = 15.0):
        if not token:
            raise McpError("no bearer token configured")
        self._url = url
        self._token = token
        self._timeout = timeout

    # -- blocking HTTP, run in a thread so the event loop stays free ---------------
    def _post(self, payload: Dict[str, Any], protocol: Optional[str]) -> Tuple[int, bytes, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
            "Authorization": f"Bearer {self._token}",
        }
        if protocol:
            headers["MCP-Protocol-Version"] = protocol
        req = urllib.request.Request(
            self._url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise McpError(f"cannot reach MCP server ({type(exc).__name__})") from None

    def _call_tool_sync(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        status, body, ctype = self._post(
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "hueri-demo-backend", "version": "1"},
                },
            },
            None,
        )
        if status == 401:
            raise McpError("MCP server rejected the bearer token (401)")
        if status != 200:
            raise McpError(f"MCP initialize failed (HTTP {status})")
        init = _parse_body(body, ctype)
        protocol = (init.get("result") or {}).get("protocolVersion") or PROTOCOL_VERSION

        status, _, _ = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, protocol
        )
        if status not in (200, 202):
            raise McpError(f"MCP initialized notification failed (HTTP {status})")

        status, body, ctype = self._post(
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            protocol,
        )
        if status != 200:
            raise McpError(f"MCP tools/call failed (HTTP {status})")
        msg = _parse_body(body, ctype)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise McpError(f"MCP protocol error: {str(err.get('message', 'unknown'))[:120]}")
        return _unwrap_result(msg.get("result") or {})

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._call_tool_sync, name, arguments)


async def timed_call(client: Any, name: str, arguments: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Call a tool and return (result, elapsed_ms)."""
    start = time.perf_counter()
    result = await client.call_tool(name, arguments)
    return result, int((time.perf_counter() - start) * 1000)
