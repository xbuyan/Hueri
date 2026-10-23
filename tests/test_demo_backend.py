import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from demo_backend.config import DemoSettings
from demo_backend.mcp_client import McpClient, McpError
from demo_backend.router import Intent, parse, run_intent

TOKEN = "test-token-not-real"
TENDERS = [
    {"id": 1, "title": "[SAMPLE] Digital health platform", "buyer": "Ministry of Health", "is_mock": True,
     "analysis": {"relevance_score": 8.5}, "analysis_state": "analyzed", "deadline": None},
    {"id": 2, "title": "[SAMPLE] Water sanitation works", "buyer": None, "is_mock": True,
     "analysis": None, "analysis_state": "unreviewed", "deadline": None},
    {"id": 3, "title": "[SAMPLE] Health records software", "buyer": "County Gov", "is_mock": True,
     "analysis": {"relevance_score": 6}, "analysis_state": "analyzed", "deadline": None},
]
CALLS = []


def _tool(name, args):
    CALLS.append((name, args))
    if name == "search_tenders":
        q = (args.get("query") or "").lower()
        rows = [t for t in TENDERS if q in t["title"].lower() or q in (t["buyer"] or "").lower()]
        return {"ok": True, "tenders": rows[: args.get("limit", 10)], "next_cursor": None}
    t = next((x for x in TENDERS if x["id"] == args.get("tender_id")), None)
    if t is None:
        return {"ok": False, "error": {"code": "not_found", "message": "No tender with id 99"}}
    if name == "get_tender_details":
        return {"ok": True, "tender": t}
    return {"ok": True, "tender": t, "explanation": "Strong overlap with digital health services.",
            "matched_services": ["telemedicine", "records"], "analysis": t["analysis"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401); self.end_headers(); return
        m = body.get("method")
        if m == "notifications/initialized":
            self.send_response(202); self.end_headers(); return
        if m == "initialize":
            out = {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-11-25"}}
        elif m == "tools/list":
            names = ["search_tenders", "get_tender_details", "explain_tender_match"]
            out = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": n} for n in names]}}
        else:
            p = body["params"]
            res = _tool(p["name"], p["arguments"])
            out = {"jsonrpc": "2.0", "id": body["id"], "result": {"structuredContent": res, "isError": False,
                   "content": [{"type": "text", "text": json.dumps(res)}]}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def url():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()


def run(coro):
    return asyncio.run(coro)


# --- parsing -----------------------------------------------------------------------
def test_parse_search_keywords():
    i = parse("find tenders for health in Kenya")
    assert i.name == "search" and i.words == ["health", "kenya"]


def test_parse_filters():
    i = parse("show the newest World Bank tenders")
    assert i.filters == {"source": "worldbank", "sort": "newest"}
    assert parse("tenders with score above 7").filters["min_score"] == 7.0
    assert parse("good fit tenders").filters["fit_only"] is True


def test_parse_explain_and_details_by_number_and_ordinal():
    assert parse("explain tender 3") == Intent("explain", tender_id=3)
    assert parse("why is the second one a match", {"ids": [4, 5, 6]}).tender_id == 5
    assert parse("details of the first one", {"ids": [4]}).name == "details"


def test_parse_ordinal_beyond_context_is_not_guessed():
    assert parse("explain the third one", {"ids": [4]}).name != "explain"


def test_parse_brief_and_help():
    assert parse("brief me on the best matches").name == "brief"
    assert parse("hello there").name == "help"
    assert parse("").name == "help"


def test_parse_ignores_non_int_context():
    assert parse("explain the first one", {"ids": ["x", None]}).name != "explain"


# --- client ------------------------------------------------------------------------
def test_client_roundtrip(url):
    r = run(McpClient(url, TOKEN).call_tool("get_tender_details", {"tender_id": 1}))
    assert r["tender"]["id"] == 1


def test_client_bad_token_error_hides_token(url):
    with pytest.raises(McpError) as e:
        run(McpClient(url, "wrong-secret-value").call_tool("search_tenders", {}))
    assert "401" in str(e.value) and "wrong-secret-value" not in str(e.value)


def test_client_unreachable_server():
    with pytest.raises(McpError):
        run(McpClient("http://127.0.0.1:9/mcp", TOKEN, timeout=2).call_tool("search_tenders", {}))


def test_client_requires_token():
    with pytest.raises(McpError):
        McpClient("http://x", "")


# --- router end to end ---------------------------------------------------------------
def test_search_is_labelled_sample_and_passes_include_mock(url):
    CALLS.clear()
    t = run(run_intent(parse("find tenders for health"), McpClient(url, TOKEN), include_mock=True))
    assert t.sample_data and t.reply.startswith("Sample data.")
    assert CALLS[0][1]["include_mock"] is True and CALLS[0][1]["query"] == "health"
    assert t.context["ids"] == [1, 3]
    assert "Digital health platform" in t.reply and "8.5 out of 10" in t.reply


def test_search_broadens_when_phrase_has_no_hits(url):
    CALLS.clear()
    t = run(run_intent(parse("find tenders for health in Kenya"), McpClient(url, TOKEN)))
    queries = [c[1].get("query") for c in CALLS]
    assert queries[0] == "health kenya" and "health" in queries
    assert "broadened" in t.reply and len(t.steps) >= 2


def test_search_no_results_message(url):
    t = run(run_intent(parse("find tenders for submarines"), McpClient(url, TOKEN)))
    assert "i found nothing about submarines" in t.reply.lower()
    assert "so here are the top" in t.reply.lower()
    assert t.context["ids"], "fallback listing must keep ids for follow-up questions"


def test_brief_orchestrates_three_tools(url):
    CALLS.clear()
    t = run(run_intent(parse("brief me on the best matches"), McpClient(url, TOKEN)))
    assert [c[0] for c in CALLS] == ["search_tenders", "get_tender_details", "explain_tender_match"]
    assert CALLS[1][1]["tender_id"] == 1
    assert "stored AI analysis, not verified" in t.reply
    assert t.steps[0]["tool"] == "tools/list" and t.steps[0]["ok"] is True
    assert t.steps[0]["summary"].startswith("3 tool(s):")
    assert [s["tool"] for s in t.steps[1:]] == [c[0] for c in CALLS]


def test_explain_by_ordinal_uses_context(url):
    CALLS.clear()
    i = parse("why is the first one a match", {"ids": [3, 1]})
    t = run(run_intent(i, McpClient(url, TOKEN)))
    assert CALLS == [("explain_tender_match", {"tender_id": 3})]
    assert "telemedicine" in t.reply


def test_tool_error_is_spoken_not_raised(url):
    t = run(run_intent(Intent("details", tender_id=99), McpClient(url, TOKEN)))
    assert "couldn't get tender 99" in t.reply and t.steps[-1]["ok"] is False


def test_service_down_is_spoken_not_raised():
    t = run(run_intent(Intent("details", tender_id=1), McpClient("http://127.0.0.1:9/mcp", TOKEN, timeout=2)))
    assert "couldn't reach" in t.reply.lower()
    assert t.steps[-1]["tool"] == "get_tender_details" and t.steps[-1]["ok"] is False


def test_trace_never_contains_token(url):
    t = run(run_intent(parse("brief me on the best matches"), McpClient(url, TOKEN)))
    assert TOKEN not in json.dumps([t.reply, t.steps, t.context])


# --- config --------------------------------------------------------------------------
def test_config_env_wins_over_file(tmp_path):
    f = tmp_path / "demo.env"
    f.write_text("MCP_BEARER_TOKEN=from-file\nDB_PASSWORD=zzz\n")
    s = DemoSettings.load({"DEMO_MCP_TOKEN": "from-env"}, f)
    assert s.token == "from-env" and s.token_source == "env"


def test_config_file_token_and_ambiguity(tmp_path):
    f = tmp_path / "demo.env"
    f.write_text("MCP_BEARER_TOKEN=abc\nDB_PASSWORD=zzz\n")
    assert DemoSettings.load({}, f).token == "abc"
    f.write_text("FOO_TOKEN=a\nBAR_TOKEN=b\n")
    s = DemoSettings.load({}, f)
    assert s.token is None and s.token_source == "missing"


def test_config_missing_file_and_flags(tmp_path):
    s = DemoSettings.load({"DEMO_INCLUDE_MOCK": "false"}, tmp_path / "nope.env")
    assert s.token is None and s.include_mock is False
