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


def test_apply_script_is_idempotent(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parent.parent
    app_py = tmp_path / "demo_backend" / "app.py"
    app_py.parent.mkdir()
    app_py.write_text("app = object()\n")
    src = (root / "scripts" / "apply_chunk2.py").read_text().replace(
        "Path(__file__).resolve().parent.parent", "Path(%r)" % str(tmp_path))
    exec(compile(src, "apply_chunk2", "exec"), {"__file__": str(root / "scripts" / "apply_chunk2.py"), "Path": Path})
    once = app_py.read_text()
    exec(compile(src, "apply_chunk2", "exec"), {"__file__": str(root / "scripts" / "apply_chunk2.py"), "Path": Path})
    assert app_py.read_text() == once
    assert once.count("mount_web(app)") == 1
