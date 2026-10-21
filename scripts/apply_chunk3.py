"""Idempotent patches: (1) retry-with-backoff around provisioning in `mcp_demo.py up`,
(2) group the page's MCP trace by request. Run from the repo root."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent if "__file__" in globals() else Path(".")


def patch(path, old, new, marker):
    p = ROOT / path
    s = p.read_text()
    if marker in s:
        print("already patched:", path, marker)
        return
    assert s.count(old) == 1, f"{path}: pattern not found exactly once: {old[:50]!r}"
    p.write_text(s.replace(old, new))
    print("patched:", path)


# 1. cold-container race in `up`
patch(
    "scripts/mcp_demo.py",
    "    asyncio.run(_provision(ro_password))\n",
    "    _with_retries(lambda: asyncio.run(_provision(ro_password)), what=\"provisioning\")\n",
    "_with_retries(lambda",
)
patch(
    "scripts/mcp_demo.py",
    "def cmd_up(args) -> None:\n",
    'def _with_retries(fn, *, what, attempts=6, first_delay=1.0):\n'
    '    """Retry on any error with exponential backoff (a cold Postgres container can reset\n'
    '    the first connections even after it reports ready)."""\n'
    "    import time\n"
    "    delay = first_delay\n"
    "    for n in range(1, attempts + 1):\n"
    "        try:\n"
    "            return fn()\n"
    "        except Exception as exc:  # noqa: BLE001\n"
    "            if n == attempts:\n"
    "                raise\n"
    '            print(f"{what}: attempt {n} failed ({type(exc).__name__}); retrying in {delay:.0f}s")\n'
    "            time.sleep(delay)\n"
    "            delay = min(delay * 2, 8.0)\n"
    "\n\n"
    "def cmd_up(args) -> None:\n",
    "def _with_retries(",
)

# 2. trace grouped by request
html = ROOT / "demo_backend/web/index.html"
s = html.read_text()
if "class=\"req\"" in s or ".req{" in s:
    print("already patched: index.html")
else:
    a = s.index("  function renderTrace(steps){")
    b = s.index("  function speak(text){")
    new_fn = '''  function renderTrace(steps,question){
    traceEmpty.style.display="none";
    var h=document.createElement("li"); h.className="req"; h.textContent="You asked: "+question;
    trace.appendChild(h);
    (steps||[]).forEach(function(s){
      var li=document.createElement("li"); li.className="step"+(s.ok?"":" fail");
      var n=document.createElement("div"); n.className="name"; n.textContent=s.tool;
      var m=document.createElement("div"); m.className="meta";
      m.textContent=(s.ok?"ok":"failed")+(s.ms!=null?" in "+s.ms+" ms":"")+(s.summary?", "+s.summary:"");
      var p=document.createElement("pre"); p.textContent=JSON.stringify(s.arguments||{},null,2);
      li.appendChild(n); li.appendChild(m); li.appendChild(p);
      if(s.error){var e=document.createElement("div"); e.className="meta"; e.textContent=s.error; li.appendChild(e);}
      trace.appendChild(li);
    });
    trace.lastElementChild&&trace.lastElementChild.scrollIntoView({block:"nearest"});
  }
'''
    s = s[:a] + new_fn + s[b:]
    assert s.count("renderTrace(d.steps);") == 1
    s = s.replace("renderTrace(d.steps);", "renderTrace(d.steps,text);")
    assert s.count(".step .meta{") == 1
    s = s.replace(".step .meta{", ".req{font-weight:600;margin-top:6px;padding-top:10px;border-top:1px solid var(--line)}\n.step .meta{", 1)
    html.write_text(s)
    print("patched: index.html")
