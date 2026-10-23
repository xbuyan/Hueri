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
from typing import Any, Dict, List, Optional, Tuple

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


def _validate_response_id(message: Dict[str, Any], expected_id: int) -> None:
    response_id = message.get("id")
    if type(response_id) is not int or response_id != expected_id:
        raise McpError("MCP server returned an invalid response ID")


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

    def _rpc_sync(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """initialize -> notifications/initialized -> one JSON-RPC request; returns its result."""
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
        _validate_response_id(init, 1)
        if "error" in init:
            raise McpError("MCP initialize returned a protocol error")
        init_result = init.get("result")
        if not isinstance(init_result, dict):
            raise McpError("MCP initialize returned an unexpected response shape")
        protocol = init_result.get("protocolVersion")
        if not isinstance(protocol, str) or not protocol:
            raise McpError("MCP initialize returned an invalid protocol version")

        status, _, _ = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, protocol
        )
        if status not in (200, 202):
            raise McpError(f"MCP initialized notification failed (HTTP {status})")

        status, body, ctype = self._post(
            {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}, protocol
        )
        if status != 200:
            raise McpError(f"MCP {method} failed (HTTP {status})")
        msg = _parse_body(body, ctype)
        _validate_response_id(msg, 2)
        if "error" in msg:
            raise McpError(f"MCP {method} returned a protocol error")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise McpError(f"MCP {method} returned an unexpected response shape")
        return result

    def _call_tool_sync(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return _unwrap_result(self._rpc_sync("tools/call", {"name": name, "arguments": arguments}))

    def _list_tools_sync(self) -> List[str]:
        tools = self._rpc_sync("tools/list", {}).get("tools")
        if not isinstance(tools, list):
            raise McpError("MCP tools/list returned no tools array")
        if any(
            not isinstance(tool, dict)
            or not isinstance(tool.get("name"), str)
            or not tool["name"]
            for tool in tools
        ):
            raise McpError("MCP tools/list returned an invalid tools array")
        return [tool["name"] for tool in tools]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._call_tool_sync, name, arguments)

    async def list_tools(self) -> List[str]:
        """Real MCP tools/list (names only) over the same Streamable HTTP endpoint."""
        return await asyncio.to_thread(self._list_tools_sync)


async def timed_call(client: Any, name: str, arguments: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Call a tool and return (result, elapsed_ms)."""
    start = time.perf_counter()
    result = await client.call_tool(name, arguments)
    return result, int((time.perf_counter() - start) * 1000)


async def timed_list_tools(client: Any) -> Tuple[List[str], int]:
    """Run MCP tools/list and return (tool names, elapsed_ms)."""
    start = time.perf_counter()
    names = await client.list_tools()
    return names, int((time.perf_counter() - start) * 1000)
