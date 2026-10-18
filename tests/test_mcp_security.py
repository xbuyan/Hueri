"""Tests for the MCP security gate and settings. Standard library only."""
import asyncio
import logging
import os
import subprocess
import sys
import unittest
from pathlib import Path

from app.mcp_server.security import SecurityGate
from app.mcp_server.settings import ConfigError, McpSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "x" * 40
HOST = "mcp.example.org"
ORIGIN = "https://app.example.org"


def make_settings(**overrides):
    env = {
        "MCP_BEARER_TOKEN": TOKEN,
        "MCP_ALLOWED_HOSTS": HOST,
        "MCP_ALLOWED_ORIGINS": ORIGIN,
    }
    env.update(overrides)
    return McpSettings.from_environ({k: v for k, v in env.items() if v is not None})


class Downstream:
    """Stands in for the MCP app; the call counter is the sentinel."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            self.calls += 0
            return
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def call(gate, headers, query=b"", scope_type="http"):
    scope = {
        "type": scope_type,
        "method": "POST",
        "path": "/mcp",
        "query_string": query,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(gate(scope, receive, send))
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    if start is None:
        return None, {}
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}


def good_headers(**extra):
    headers = [("host", HOST), ("authorization", f"Bearer {TOKEN}")]
    headers.extend(extra.items())
    return headers


class SettingsTests(unittest.TestCase):
    def test_valid_settings(self):
        s = make_settings()
        self.assertEqual(s.allowed_hosts, (HOST,))
        self.assertEqual(s.allowed_origins, (ORIGIN,))
        self.assertFalse(s.dev_mode)

    def test_missing_or_short_token_fails(self):
        for token in (None, "", "short", "x" * 31, "has space " + "x" * 40):
            with self.subTest(token=token):
                with self.assertRaises(ConfigError):
                    make_settings(MCP_BEARER_TOKEN=token)

    def test_missing_hosts_fails_closed_in_production(self):
        for value in (None, "", " , "):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    make_settings(MCP_ALLOWED_HOSTS=value)

    def test_wildcards_are_rejected_even_in_dev_mode(self):
        for kwargs in (
            {"MCP_ALLOWED_HOSTS": "*"},
            {"MCP_ALLOWED_HOSTS": "*.example.org", "MCP_DEV_MODE": "1"},
            {"MCP_ALLOWED_ORIGINS": "*"},
            {"MCP_ALLOWED_ORIGINS": "https://*.example.org"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ConfigError):
                    make_settings(**kwargs)

    def test_dev_mode_supplies_local_default_hosts(self):
        s = make_settings(MCP_ALLOWED_HOSTS=None, MCP_DEV_MODE="1", MCP_PORT="9001")
        self.assertEqual(s.allowed_hosts, ("localhost:9001", "127.0.0.1:9001"))

    def test_dev_mode_requires_exactly_one(self):
        with self.assertRaises(ConfigError):
            make_settings(MCP_ALLOWED_HOSTS=None, MCP_DEV_MODE="true")

    def test_empty_origins_allowed(self):
        self.assertEqual(make_settings(MCP_ALLOWED_ORIGINS=None).allowed_origins, ())

    def test_origin_must_have_scheme(self):
        with self.assertRaises(ConfigError):
            make_settings(MCP_ALLOWED_ORIGINS="app.example.org")

    def test_trailing_slash_and_case_normalised(self):
        s = make_settings(MCP_ALLOWED_ORIGINS="HTTPS://App.Example.org/", MCP_ALLOWED_HOSTS="MCP.Example.ORG")
        self.assertEqual(s.allowed_origins, (ORIGIN,))
        self.assertEqual(s.allowed_hosts, (HOST,))

    def test_repr_hides_token(self):
        self.assertNotIn(TOKEN, repr(make_settings()))

    def test_bad_port_fails(self):
        for port in ("abc", "0", "70000"):
            with self.subTest(port=port):
                with self.assertRaises(ConfigError):
                    make_settings(MCP_PORT=port)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.down = Downstream()
        self.gate = SecurityGate(self.down, make_settings())

    def assert_rejected(self, headers, query=b"", status=401):
        before = self.down.calls
        got, response_headers = call(self.gate, headers, query)
        self.assertEqual(got, status)
        self.assertEqual(self.down.calls, before, "sentinel: downstream must not run")
        self.assertNotIn("www-authenticate", response_headers)
        return response_headers

    def test_valid_request_passes_and_sentinel_counts_exactly_one(self):
        got, _ = call(self.gate, good_headers())
        self.assertEqual(got, 200)
        self.assertEqual(self.down.calls, 1)

    def test_unrelated_query_parameter_passes(self):
        got, _ = call(self.gate, good_headers(), b"foo=bar&page=2")
        self.assertEqual(got, 200)

    def test_invalid_authorization_variants(self):
        cases = {
            "missing": [("host", HOST)],
            "basic": [("host", HOST), ("authorization", "Basic dXNlcjpwYXNz")],
            "bearer_only": [("host", HOST), ("authorization", "Bearer")],
            "bearer_empty": [("host", HOST), ("authorization", "Bearer ")],
            "bearer_double_space": [("host", HOST), ("authorization", f"Bearer  {TOKEN}")],
            "wrong_token": [("host", HOST), ("authorization", "Bearer " + "y" * 51)],
            "token_prefix": [("host", HOST), ("authorization", f"Bearer {TOKEN[:-1]}")],
            "token_suffix": [("host", HOST), ("authorization", f"Bearer {TOKEN}x")],
            "no_scheme": [("host", HOST), ("authorization", TOKEN)],
            "duplicate_header": [
                ("host", HOST),
                ("authorization", f"Bearer {TOKEN}"),
                ("authorization", f"Bearer {TOKEN}"),
            ],
            "oversized": [("host", HOST), ("authorization", "Bearer " + "z" * 5000)],
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                self.assert_rejected(headers)

    def test_scheme_is_case_insensitive(self):
        got, _ = call(self.gate, [("host", HOST), ("authorization", f"bearer {TOKEN}")])
        self.assertEqual(got, 200)

    def test_query_credentials_rejected_alone_and_with_valid_header(self):
        for query in (
            b"token=abc",
            b"access_token=abc",
            b"TOKEN=abc",
            b"Access_Token=abc",
            b"token=",
            b"foo=bar&token=abc",
        ):
            with self.subTest(query=query, header="none"):
                self.assert_rejected([("host", HOST)], query)
            with self.subTest(query=query, header="valid"):
                self.assert_rejected(good_headers(), query)

    def test_query_token_value_is_never_accepted_as_credential(self):
        self.assert_rejected([("host", HOST)], f"access_token={TOKEN}".encode())

    def test_invalid_host(self):
        for headers in (
            [("host", "evil.example"), ("authorization", f"Bearer {TOKEN}")],
            [("authorization", f"Bearer {TOKEN}")],
            [("host", HOST), ("host", HOST), ("authorization", f"Bearer {TOKEN}")],
            [("host", HOST + ":9999"), ("authorization", f"Bearer {TOKEN}")],
        ):
            with self.subTest(headers=[h[0] for h in headers]):
                self.assert_rejected(headers, status=421)

    def test_host_check_runs_before_auth(self):
        self.assert_rejected([("host", "evil.example")], status=421)

    def test_host_matching_is_case_insensitive(self):
        got, _ = call(self.gate, [("host", HOST.upper()), ("authorization", f"Bearer {TOKEN}")])
        self.assertEqual(got, 200)

    def test_origin_allowed_absent_and_disallowed(self):
        got, _ = call(self.gate, good_headers(origin=ORIGIN))
        self.assertEqual(got, 200)
        got, _ = call(self.gate, good_headers())
        self.assertEqual(got, 200)
        for bad in ("https://evil.example", "null", "http://app.example.org", ""):
            with self.subTest(origin=bad):
                self.assert_rejected(good_headers(origin=bad), status=403)

    def test_origin_check_runs_before_auth(self):
        self.assert_rejected([("host", HOST), ("origin", "https://evil.example")], status=403)

    def test_empty_origin_allowlist_rejects_any_origin_but_allows_none(self):
        gate = SecurityGate(self.down, make_settings(MCP_ALLOWED_ORIGINS=None))
        got, _ = call(gate, good_headers(origin=ORIGIN))
        self.assertEqual(got, 403)
        got, _ = call(gate, good_headers())
        self.assertEqual(got, 200)

    def test_every_rejection_leaves_sentinel_at_zero(self):
        down = Downstream()
        gate = SecurityGate(down, make_settings())
        attempts = [
            ([("host", HOST)], b""),
            ([("host", HOST), ("authorization", "Bearer nope")], b""),
            ([("host", HOST), ("authorization", f"Bearer {TOKEN}")], b"token=x"),
            ([("host", "evil.example"), ("authorization", f"Bearer {TOKEN}")], b""),
            ([("host", HOST), ("authorization", f"Bearer {TOKEN}"), ("origin", "https://evil.example")], b""),
        ]
        for headers, query in attempts:
            call(gate, headers, query)
            self.assertEqual(down.calls, 0)
        call(gate, good_headers())
        self.assertEqual(down.calls, 1)

    def test_rejection_body_does_not_echo_secrets(self):
        down = Downstream()
        gate = SecurityGate(down, make_settings())
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "method": "POST", "path": "/mcp",
            "query_string": f"access_token={TOKEN}".encode(),
            "headers": [(b"host", HOST.encode()), (b"authorization", f"Bearer {TOKEN}".encode())],
        }
        asyncio.run(gate(scope, receive, send))
        blob = repr(sent)
        self.assertNotIn(TOKEN, blob)

    def test_token_never_logged(self):
        with self.assertLogs("hueri.mcp.security", level="WARNING") as captured:
            call(self.gate, [("host", HOST), ("authorization", f"Bearer {TOKEN}x")], f"access_token={TOKEN}".encode())
            call(self.gate, [("host", HOST), ("authorization", f"Bearer {TOKEN}x")])
        text = "\n".join(captured.output)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Bearer", text)

    def test_lifespan_passes_through(self):
        calls = []

        async def app(scope, receive, send):
            calls.append(scope["type"])

        gate = SecurityGate(app, make_settings())
        asyncio.run(gate({"type": "lifespan"}, None, None))
        self.assertEqual(calls, ["lifespan"])

    def test_websocket_is_closed_not_forwarded(self):
        sent = []

        async def send(message):
            sent.append(message)

        asyncio.run(self.gate({"type": "websocket", "headers": []}, None, send))
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        self.assertEqual(self.down.calls, 0)


class ImportBoundaryTests(unittest.TestCase):
    def test_only_expected_app_modules_are_loaded(self):
        code = (
            "import sys\n"
            "import app.mcp_server.security, app.mcp_server.settings\n"
            "print(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.')))\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = set(eval(result.stdout.strip()))
        self.assertEqual(
            loaded,
            {"app", "app.mcp_server", "app.mcp_server.security", "app.mcp_server.settings"},
        )


if __name__ == "__main__":
    unittest.main()
