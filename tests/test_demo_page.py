import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from demo_backend.web_mount import INDEX, mount_web


def _client():
    return TestClient(mount_web(FastAPI()))


def test_root_and_ui_serve_html():
    c = _client()
    for path in ("/", "/ui"):
        r = c.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Live MCP calls" in r.text


def test_page_shows_sample_data_badge_and_calls_ask():
    html = INDEX.read_text()
    assert "Sample data" in html
    assert "/api/ask" in html
    assert "sample_data" in html


def test_page_has_no_external_resources_and_no_innerhtml():
    html = INDEX.read_text()
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", html)
    assert "innerHTML" not in html
    assert "localStorage" not in html
