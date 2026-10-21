from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_trace_is_grouped_by_request():
    html = (ROOT / "demo_backend/web/index.html").read_text()
    assert "renderTrace(d.steps,text)" in html
    assert 'h.className="req"' in html
    assert "innerHTML" not in html


def test_up_retries_provisioning():
    src = (ROOT / "scripts/mcp_demo.py").read_text()
    assert "def _with_retries(" in src
    assert "_with_retries(lambda: asyncio.run(_provision(ro_password))" in src


def test_with_retries_backs_off(monkeypatch):
    import importlib.util
    import time
    spec = importlib.util.spec_from_file_location("mcp_demo", ROOT / "scripts/mcp_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError("cold start")
        return "ok"

    assert mod._with_retries(flaky, what="x") == "ok"
    assert sleeps == [1.0, 2.0]
