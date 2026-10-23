"""MCP discovery (tools/list): client protocol order and router trace behaviour."""
import asyncio
import json

import pytest

from demo_backend.mcp_client import McpClient, McpError
from demo_backend.router import parse, run_intent

TOOLS = ["search_tenders", "get_tender_details", "explain_tender_match"]
ROW = {"tenders": [{"id": 7, "title": "Health project", "buyer": "B"}]}


class _Stub(McpClient):
    def __init__(self, *, response_ids=None, init_result=None, list_error=None, call_error=None):
        super().__init__("http://stub/mcp", "secret-token")
        self.sent = []
        self.response_ids = {"initialize": 1, "tools/list": 2, "tools/call": 2}
        self.response_ids.update(response_ids or {})
        self.init_result = (
            {"protocolVersion": "2025-11-25"} if init_result is None else init_result
        )
        self.list_error = list_error
        self.call_error = call_error

    def _post(self, payload, protocol):
        method = payload.get("method")
        self.sent.append((method, protocol, payload.get("id")))
        if method == "initialize":
            body = {"jsonrpc": "2.0", "result": self.init_result}
            self.init_result = {"protocolVersion": "2025-11-25"}
        elif method == "notifications/initialized":
            return 202, b"", ""
        elif method == "tools/list":
            if self.list_error is not None:
                body = {"jsonrpc": "2.0", "error": {"code": -32000, "message": self.list_error}}
            else:
                body = {"jsonrpc": "2.0", "result": {"tools": [{"name": n} for n in TOOLS]}}
        elif method == "tools/call":
            if self.call_error is not None:
                body = {"jsonrpc": "2.0", "error": {"code": -32000, "message": self.call_error}}
            else:
                body = {"jsonrpc": "2.0", "result": {"structuredContent": ROW, "isError": False}}
        else:
            raise AssertionError(method)
        response_id = self.response_ids[method]
        if response_id is not None:
            body["id"] = response_id
        return 200, json.dumps(body).encode(), "application/json"


class _Fake:
    def __init__(self, fail_discovery=False):
        self.fail = fail_discovery

    async def list_tools(self):
        if self.fail:
            raise McpError("discovery boom")
        return TOOLS

    async def call_tool(self, name, arguments):
        return ROW


class _UnexpectedDiscovery:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        raise RuntimeError("CANARY_UNTRUSTED_DISCOVERY_ERROR")

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return ROW


class _BadSummaryDiscovery(_UnexpectedDiscovery):
    async def list_tools(self):
        return [object()]


class _NoDiscovery:
    async def call_tool(self, name, arguments):
        return ROW


def _turn(client, text="find tenders for health"):
    return asyncio.run(run_intent(parse(text), client))


def test_client_lists_tools_in_protocol_order():
    c = _Stub()
    assert asyncio.run(c.list_tools()) == TOOLS
    assert [m for m, _, _ in c.sent] == ["initialize", "notifications/initialized", "tools/list"]
    assert [request_id for _, _, request_id in c.sent] == [1, None, 2]
    assert all(proto == "2025-11-25" for _, proto, _ in c.sent[1:])


def test_client_rejects_mismatched_response_id():
    with pytest.raises(McpError, match="invalid response ID"):
        asyncio.run(_Stub(response_ids={"tools/list": 99}).list_tools())


def test_client_rejects_missing_response_id():
    with pytest.raises(McpError, match="invalid response ID"):
        asyncio.run(_Stub(response_ids={"tools/list": None}).list_tools())


def test_client_rejects_mismatched_initialization_response_id():
    with pytest.raises(McpError, match="invalid response ID"):
        asyncio.run(_Stub(response_ids={"initialize": 2}).list_tools())


def test_client_rejects_invalid_initialization_response_shape():
    with pytest.raises(McpError, match="unexpected response shape"):
        asyncio.run(_Stub(init_result=[]).list_tools())


def test_invalid_initialization_shape_does_not_abort_requested_operation():
    t = _turn(_Stub(init_result=[]))
    assert t.steps[0]["tool"] == "tools/list" and t.steps[0]["ok"] is False
    assert t.steps[1]["tool"] == "search_tenders" and t.steps[1]["ok"] is True
    assert "Health project" in t.reply


def test_discovery_step_is_first_in_trace():
    t = _turn(_Fake())
    assert [s["tool"] for s in t.steps][:2] == ["tools/list", "search_tenders"]
    assert t.steps[0]["ok"] is True
    assert all(n in t.steps[0]["summary"] for n in TOOLS)


def test_discovery_failure_does_not_block_request():
    t = _turn(_Fake(fail_discovery=True))
    assert t.steps[0]["tool"] == "tools/list" and t.steps[0]["ok"] is False
    assert t.steps[1]["tool"] == "search_tenders"
    assert "Health project" in t.reply


def test_unexpected_discovery_exception_does_not_abort_requested_operation():
    client = _UnexpectedDiscovery()
    t = _turn(client)
    assert client.calls == [("search_tenders", {"include_mock": True, "limit": 5, "query": "health"})]
    assert t.steps[0] == {
        "tool": "tools/list", "arguments": {}, "ok": False,
        "error": "MCP discovery failed", "summary": "failed",
    }
    assert "Health project" in t.reply
    assert "CANARY_UNTRUSTED_DISCOVERY_ERROR" not in json.dumps([t.reply, t.steps])


def test_invalid_discovery_result_summary_does_not_abort_requested_operation():
    client = _BadSummaryDiscovery()
    t = _turn(client)
    assert client.calls == [("search_tenders", {"include_mock": True, "limit": 5, "query": "health"})]
    assert t.steps[0] == {
        "tool": "tools/list", "arguments": {}, "ok": False,
        "error": "MCP discovery failed", "summary": "failed",
    }
    assert "Health project" in t.reply


def test_server_error_text_is_redacted_from_public_trace():
    secret_like = "CANARY_SERVER_SECRET_83914"
    c = _Stub(list_error=secret_like)
    t = _turn(c)
    public_response = json.dumps({"reply": t.reply, "steps": t.steps, "context": t.context})
    assert secret_like not in public_response
    assert t.steps[0]["error"] == "MCP discovery failed"
    assert t.steps[1]["tool"] == "search_tenders" and t.steps[1]["ok"] is True
    assert "Health project" in t.reply


def test_tool_error_text_is_redacted_from_public_trace():
    secret_like = "CANARY_TOOL_ERROR_SECRET_92177"
    c = _Stub(call_error=secret_like)
    t = _turn(c)
    public_response = json.dumps({"reply": t.reply, "steps": t.steps, "context": t.context})
    assert secret_like not in public_response
    assert t.steps[0]["ok"] is True
    assert t.steps[1]["tool"] == "search_tenders" and t.steps[1]["ok"] is False
    assert "MCP tools/call returned a protocol error" in t.steps[1]["error"]


def test_help_makes_no_mcp_calls():
    assert _turn(_Fake(), "hello").steps == []


def test_clients_without_list_tools_are_unchanged():
    assert [s["tool"] for s in _turn(_NoDiscovery()).steps] == ["search_tenders"]
