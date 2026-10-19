"""Tests for the MCP server wiring: protocol behaviour, gate ordering, tool exposure.

The protocol tests need the mcp SDK (pip install mcp==1.30.0) and starlette's
TestClient (httpx). They use a temporary SQLite file as the data source; the
Postgres role tests are separate. Tests that need the SDK are skipped, with a
reason, when it is not installed.
"""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.mcp_server.db import database_url_from_env
from app.mcp_server.settings import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
HAS_SDK = importlib.util.find_spec("mcp") is not None
HAS_CLIENT = importlib.util.find_spec("httpx") is not None
TOKEN = "test-token-" + "x" * 40
HOST = "mcp.example.org"
ENV = {
    "MCP_BEARER_TOKEN": TOKEN,
    "MCP_ALLOWED_HOSTS": HOST,
    "MCP_ALLOWED_ORIGINS": "https://app.example.org",
    "MCP_DATABASE_URL": "postgresql+asyncpg://ro:unused@127.0.0.1:1/none",
}
EXPECTED_TOOLS = {"search_tenders", "get_tender_details", "explain_tender_match"}


class DatabaseUrlTests(unittest.TestCase):
    def test_requires_asyncpg_postgres_url(self):
        for value in (None, "", "   ", "sqlite:///x.db", "postgresql://u:p@h/db", "mysql+aiomysql://u@h/d"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    database_url_from_env({} if value is None else {"MCP_DATABASE_URL": value})

    def test_accepts_asyncpg_url(self):
        url = "postgresql+asyncpg://ro:pw@127.0.0.1:55432/db"
        self.assertEqual(database_url_from_env({"MCP_DATABASE_URL": url}), url)


class SourceGuardTests(unittest.TestCase):
    def test_server_module_does_not_defer_annotations(self):
        """FastMCP resolves tool annotations with eval_str=True; names imported inside
        build_mcp cannot be found when annotations are deferred to strings."""
        source = (REPO_ROOT / "app" / "mcp_server" / "server.py").read_text()
        code_lines = [ln.strip() for ln in source.splitlines() if not ln.strip().startswith(("#", "NOTE"))]
        self.assertNotIn("from __future__ import annotations", code_lines)


class FailClosedTests(unittest.TestCase):
    def create(self, **overrides):
        from app.mcp_server.server import create_app

        env = dict(ENV)
        env.update(overrides)
        return create_app({k: v for k, v in env.items() if v is not None}, session_factory=lambda: None)

    def test_missing_security_settings_fail_before_the_sdk_loads(self):
        for key in ("MCP_BEARER_TOKEN", "MCP_ALLOWED_HOSTS"):
            with self.subTest(missing=key):
                with self.assertRaises(ConfigError):
                    self.create(**{key: None})

    def test_missing_database_url_fails_when_no_factory_injected(self):
        from app.mcp_server.server import create_app

        env = {k: v for k, v in ENV.items() if k != "MCP_DATABASE_URL"}
        with self.assertRaises(ConfigError):
            create_app(env)


def make_sqlite_file():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.models import Base, Tender, TenderAnalysis

    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    engine = create_engine(f"sqlite:///{handle.name}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            Tender(id=1, external_id="a", source="ungm", title="Road assessment", buyer="UNOPS",
                   url="https://example.org/1", deadline_str="24-Sep-2026 03:15 (GMT -4.00)",
                   raw_summary="Summary", created_at=dt.datetime(2026, 10, 1)),
            Tender(id=2, external_id="b", source="ungm", title="Unscored tender", url="https://example.org/2",
                   created_at=dt.datetime(2026, 10, 2)),
        ])
        s.flush()
        s.add(TenderAnalysis(tender_id=1, relevance_score=8.0, is_fit=True, executive_summary="Good.",
                             matched_services=["ESIA"], eligibility_gaps=[], suggested_next_steps=["Bid"]))
        s.commit()
    engine.dispose()
    return handle.name


class CountingFactory:
    """Counts how many database sessions the app opens: the sentinel for 'no work before auth'."""

    def __init__(self, inner):
        self.inner = inner
        self.opened = 0

    def __call__(self):
        self.opened += 1
        return self.inner()


@unittest.skipUnless(HAS_SDK and HAS_CLIENT, "needs the mcp SDK (pip install mcp==1.30.0) and httpx")
class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from starlette.testclient import TestClient

        from app.mcp_server.server import create_app

        cls.db_path = make_sqlite_file()
        cls.engine = create_async_engine(f"sqlite+aiosqlite:///{cls.db_path}")
        cls.factory = CountingFactory(async_sessionmaker(cls.engine, expire_on_commit=False))
        app = create_app(dict(ENV), session_factory=cls.factory)
        cls.client_cm = TestClient(app, base_url=f"http://{HOST}")
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)
        os.unlink(cls.db_path)

    def headers(self, **extra):
        h = {
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-11-25",
        }
        h.update(extra)
        return {k: v for k, v in h.items() if v is not None}

    def rpc(self, method, params=None, *, headers=None, rid=1):
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post("/mcp", json=body, headers=headers or self.headers())

    def call(self, name, arguments):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments}, rid=7)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            payload = json.loads(result["content"][0]["text"])
        if "ok" not in payload and isinstance(payload.get("result"), dict):
            payload = payload["result"]  # some SDK versions wrap dict returns under "result"
        return result, payload

    def test_initialize_negotiates_protocol_and_issues_no_session(self):
        response = self.rpc("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "tests", "version": "1"},
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["protocolVersion"], "2025-11-25")
        self.assertNotIn("mcp-session-id", {k.lower() for k in response.headers})

    def test_initialized_notification_is_accepted(self):
        response = self.client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 202, response.text)

    def test_exactly_three_read_only_tools_are_exposed(self):
        response = self.rpc("tools/list")
        self.assertEqual(response.status_code, 200, response.text)
        listed = response.json()["result"]["tools"]
        self.assertEqual({t["name"] for t in listed}, EXPECTED_TOOLS)
        for tool in listed:
            self.assertTrue(tool.get("description"))
            self.assertIs(tool.get("annotations", {}).get("readOnlyHint"), True, tool["name"])

    def test_search_tool_returns_seeded_rows(self):
        _, payload = self.call("search_tenders", {})
        self.assertTrue(payload["ok"], payload)
        self.assertEqual([t["id"] for t in payload["tenders"]], [1, 2])
        self.assertNotIn("raw_summary", json.dumps(payload))

    def test_details_and_not_found(self):
        _, payload = self.call("get_tender_details", {"tender_id": 1})
        self.assertEqual(payload["tender"]["analysis"]["relevance_score"], 8.0)
        _, missing = self.call("get_tender_details", {"tender_id": 999})
        self.assertEqual(missing["error"]["code"], "not_found")

    def test_explain_reports_unanalyzed_honestly(self):
        _, payload = self.call("explain_tender_match", {"tender_id": 2})
        self.assertEqual(payload["analysis_state"], "pending_or_failed")
        self.assertIsNone(payload["assessment"])

    def test_out_of_range_arguments_are_rejected(self):
        response = self.rpc("tools/call", {"name": "search_tenders", "arguments": {"limit": 99}}, rid=9)
        body = response.json()
        structured = body.get("result", {}).get("structuredContent") or {}
        structured = structured.get("result", structured) if isinstance(structured, dict) else {}
        rejected = "error" in body or body["result"].get("isError") is True or structured.get("ok") is False
        self.assertTrue(rejected, body)

    def test_rejected_requests_never_open_a_database_session(self):
        before = self.factory.opened
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search_tenders", "arguments": {}}}
        cases = [
            ("no auth", {"Authorization": None}, "/mcp", 401),
            ("wrong token", {"Authorization": "Bearer " + "y" * 51}, "/mcp", 401),
            ("query token", {}, "/mcp?access_token=abc", 401),
            ("bad origin", {"Origin": "https://evil.example"}, "/mcp", 403),
            ("bad host", {"Host": "evil.example"}, "/mcp", 421),
        ]
        for name, extra, path, status in cases:
            with self.subTest(case=name):
                response = self.client.post(path, json=body, headers=self.headers(**extra))
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotIn("www-authenticate", {k.lower() for k in response.headers})
        self.assertEqual(self.factory.opened, before, "a rejected request reached the database layer")
        self.call("search_tenders", {})
        self.assertEqual(self.factory.opened, before + 1, "positive control: a valid call opens one session")

    def test_protocol_version_header_behaviour(self):
        init = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
        # initialize is exempt from the header check: the version is negotiated in the body.
        for header in ("1999-01-01", None):
            with self.subTest(initialize_header=header):
                response = self.rpc("initialize", init, headers=self.headers(**{"MCP-Protocol-Version": header}))
                self.assertEqual(response.status_code, 200, response.text)
        # Every later request is checked: an unsupported version is refused, a missing one tolerated.
        wrong = self.rpc("tools/list", headers=self.headers(**{"MCP-Protocol-Version": "1999-01-01"}))
        self.assertEqual(wrong.status_code, 400, wrong.text)
        missing = self.rpc("tools/list", headers=self.headers(**{"MCP-Protocol-Version": None}))
        self.assertEqual(missing.status_code, 200, missing.text)

    def test_get_and_delete_are_refused_before_reaching_the_sdk(self):
        before = self.factory.opened
        anonymous = self.client.get("/mcp", headers={"Accept": "text/event-stream"})
        self.assertEqual(anonymous.status_code, 401)
        for method in ("get", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)("/mcp", headers=self.headers())
                self.assertEqual(response.status_code, 405, response.text)
                self.assertEqual(response.headers.get("allow"), "POST")
        self.assertEqual(self.factory.opened, before)


@unittest.skipUnless(HAS_SDK, "needs the mcp SDK (pip install mcp==1.30.0)")
class ImportBoundaryTests(unittest.TestCase):
    def test_building_the_app_loads_only_expected_app_modules(self):
        code = (
            "import sys\n"
            "from app.mcp_server.server import create_app\n"
            "env = {'MCP_BEARER_TOKEN': 'x' * 40, 'MCP_ALLOWED_HOSTS': 'mcp.example.org'}\n"
            "create_app(env, session_factory=lambda: None)\n"
            "print(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.')))\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            set(eval(result.stdout.strip().splitlines()[-1])),
            {"app", "app.models", "app.mcp_server", "app.mcp_server.server", "app.mcp_server.security",
             "app.mcp_server.settings", "app.mcp_server.db", "app.mcp_server.tools",
             "app.mcp_server.repository", "app.mcp_server.serialize"},
        )


if __name__ == "__main__":
    unittest.main()
