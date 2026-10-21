#!/usr/bin/env python3
"""Patch 3: extract shared retry/rate-limit/pagination logic from ungm.py
into a new app/collectors/base.py (RateLimitedCollector), so the next
source (PPIP, World Bank, AfDB) can inherit it instead of re-learning
tonight's UNGM lessons from scratch.

Run from the project root (venv active):  python apply_patch3.py
Requires patch 1 (unreviewed bucket) and patch 2 (rate-limit safety) to be
applied already. Does NOT contact UNGM.

This is a full-file replacement for app/collectors/ungm.py, not a line
patch: the refactor touches too much of the file for anchored edits to be
safe. Before writing anything, the script checks that your current
ungm.py still has the expected patch-1/patch-2 markers, and that main.py
only calls the collector through the public methods this rewrite keeps
(collect, fetch_recent_notices, get_mock_data) - if it calls anything
else, it aborts rather than silently breaking main.py.

Behavior is unchanged: same UNGM request shape, same antiforgery token
handling, same relevance/country/deadline filters, same unreviewed
bucket, same 429/4xx/pagination rules as patch 2. Only the retry/
rate-limit/pagination code moved to base.py, reusable by future sources.

Files touched:
  app/collectors/base.py   - new file (created)
  app/collectors/ungm.py   - rewritten (backup: ungm.py.bak-patch3)
"""
import sys
import shutil
from pathlib import Path

ROOT = Path.cwd()
UNGM = ROOT / "app/collectors/ungm.py"
BASE = ROOT / "app/collectors/base.py"
MAIN = ROOT / "main.py"
HERE = Path(__file__).parent

if not UNGM.exists():
    sys.exit("ABORTED: app/collectors/ungm.py not found. Run from the project root.")
if BASE.exists():
    sys.exit("ABORTED: app/collectors/base.py already exists - patch 3 looks already applied.")

current = UNGM.read_text(encoding="utf-8")
required_markers = ["MAX_UNREVIEWED", "RATE_LIMIT_COOLDOWN_MINUTES", "MAX_PAGES_PER_TERM"]
missing = [m for m in required_markers if m not in current]
if missing:
    sys.exit(
        "ABORTED: app/collectors/ungm.py is missing "
        f"{missing} - patch 1 and/or patch 2 don't look applied yet. Apply those first."
    )

# Guard against main.py calling something on the collector this rewrite
# doesn't keep. The public surface used by main.py must be exactly these.
if MAIN.exists():
    main_text = MAIN.read_text(encoding="utf-8")
    kept_public_calls = ["ungm_collector.collect", "ungm_collector.fetch_recent_notices",
                          "ungm_collector.get_mock_data", "UNGMCollectorError"]
    import re
    used = set(re.findall(r"ungm_collector\.(\w+)", main_text))
    unexpected = used - {"collect", "fetch_recent_notices", "get_mock_data"}
    if unexpected:
        sys.exit(
            f"ABORTED: main.py calls ungm_collector.{unexpected.pop()}, which this "
            "refactor doesn't preserve. Not touching anything - tell Claude and it will "
            "adjust the refactor instead of silently breaking main.py."
        )

new_base = (HERE / "base.py").read_text(encoding="utf-8")
new_ungm = (HERE / "ungm.py").read_text(encoding="utf-8")

shutil.copy2(UNGM, str(UNGM) + ".bak-patch3")
BASE.write_text(new_base, encoding="utf-8")
UNGM.write_text(new_ungm, encoding="utf-8")

print("created  app/collectors/base.py")
print("patched  app/collectors/ungm.py  (backup: ungm.py.bak-patch3)")
print("\nNo requests were sent to UNGM. main.py needs no changes.")
print("Verify:  python -m app.collectors.ungm 2>&1 | head -40")
