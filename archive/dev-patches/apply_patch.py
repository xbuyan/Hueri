#!/usr/bin/env python3
"""Adds the 'unreviewed' bucket + alerted_at column to the HUERI tender scout.

Run from the project root (~/projects/Hueri):   python apply_patch.py
- Edits app/models.py, app/collectors/ungm.py, main.py
- Every edit is anchored to exact text and asserted to match exactly once.
- If ANY anchor fails, NOTHING is written (all-or-nothing).
- Originals are saved as <file>.bak-unreviewed
"""
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()


def sub(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"ABORTED: anchor '{label}' matched {n} times (expected 1). Nothing was changed.")
    return text.replace(old, new, 1)


def patch_models(t):
    t = sub(t,
        "from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey, Enum, JSON\n",
        "from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey, Enum, JSON, false\n",
        "models import")
    t = sub(t,
        "    is_mock = Column(Boolean, default=False, nullable=False)\n",
        "    is_mock = Column(Boolean, default=False, nullable=False)\n"
        "    is_unreviewed = Column(Boolean, default=False, nullable=False, server_default=false())\n"
        "    alerted_at = Column(DateTime, nullable=True)\n",
        "models columns")
    return t


def patch_ungm(t):
    t = sub(t, "from typing import List, Dict, Any, Optional\n",
               "from typing import List, Dict, Any, Optional, Tuple\n", "ungm typing import")
    t = sub(t,
        "MAX_CANDIDATES = 40        # cap on what is returned (and so on what can be scored)\n",
        "MAX_CANDIDATES = 40        # cap on what is returned (and so on what can be scored)\n"
        "MAX_UNREVIEWED = 100       # cap on saved-but-unscored notices per run\n",
        "ungm MAX_UNREVIEWED")
    t = sub(t,
        "        kept: List[Dict[str, Any]] = []\n        dropped =",
        "        kept: List[Dict[str, Any]] = []\n        unreviewed: List[Dict[str, Any]] = []\n        dropped =",
        "ungm unreviewed list")
    t = sub(t,
        "                dropped[reason] += 1\n            else:\n",
        "                dropped[reason] += 1\n"
        "                if reason == \"relevance\":  # passed deadline + country, failed keyword match\n"
        "                    unreviewed.append(notice)\n"
        "            else:\n",
        "ungm collect unreviewed")
    t = sub(t,
        "        kept = kept[:MAX_CANDIDATES]\n",
        "        kept = kept[:MAX_CANDIDATES]\n"
        "        unreviewed.sort(key=lambda n: _parse_ungm_date(n.get(\"published_str\")) or date.min, reverse=True)\n"
        "        unreviewed = unreviewed[:MAX_UNREVIEWED]\n",
        "ungm caps")
    t = sub(t,
        "        return kept\n\n    def _parse_response",
        "        logger.info(\"UNGM: %d unreviewed notices (country/deadline OK, keyword match failed)\", len(unreviewed))\n"
        "        return kept, unreviewed\n\n    def _parse_response",
        "ungm return")
    t = sub(t,
        "    def fetch_recent_notices(self, page_size: int = PAGE_SIZE_PER_TERM) -> List[Dict[str, Any]]:\n",
        "    def fetch_recent_notices(self, page_size: int = PAGE_SIZE_PER_TERM) -> List[Dict[str, Any]]:\n"
        "        \"\"\"Backward-compatible wrapper: only the keyword-matched candidates.\"\"\"\n"
        "        return self.collect(page_size)[0]\n\n"
        "    def collect(\n"
        "        self, page_size: int = PAGE_SIZE_PER_TERM\n"
        "    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:\n"
        "        \"\"\"Returns (candidates_to_score, unreviewed). See fetch_recent_notices docs below.\"\"\"\n",
        "ungm collect def")
    t = sub(t,
        "    found = ungm_collector.fetch_recent_notices()\n",
        "    found, unreviewed = ungm_collector.collect()\n",
        "ungm main preview")
    t = t.rstrip("\n") + (
        "\n    print(f\"\\n{len(unreviewed)} unreviewed (passed country/deadline, failed keyword match):\\n\")\n"
        "    for n in unreviewed[:25]:\n"
        "        print(\n"
        "            f\"{(n.get('published_str') or '?'):>11} | {(n.get('country') or '?')[:18]:<18} | {n['title'][:90]}\"\n"
        "        )\n"
    )
    return t


def patch_main(t):
    t = sub(t,
        "    is_mock_run = False\n    try:\n",
        "    is_mock_run = False\n    unreviewed_notices: list = []\n    try:\n",
        "main init")
    t = sub(t,
        "        fetched_notices = await asyncio.to_thread(ungm_collector.fetch_recent_notices)\n",
        "        fetched_notices, unreviewed_notices = await asyncio.to_thread(ungm_collector.collect)\n",
        "main collect call")
    t = sub(t,
        "    scored = 0\n    unscored = 0\n",
        "    # Save notices that passed country/deadline but failed the keyword match.\n"
        "    # No Gemini spend; done before scoring so a later scoring failure can't lose them.\n"
        "    saved_unreviewed = 0\n"
        "    if unreviewed_notices:\n"
        "        ids = [n[\"external_id\"] for n in unreviewed_notices]\n"
        "        existing_res = await db.execute(select(Tender.external_id).where(Tender.external_id.in_(ids)))\n"
        "        existing_ids = set(existing_res.scalars().all())\n"
        "        for n in unreviewed_notices:\n"
        "            if n[\"external_id\"] in existing_ids:\n"
        "                continue\n"
        "            db.add(\n"
        "                Tender(\n"
        "                    external_id=n[\"external_id\"],\n"
        "                    source=n[\"source\"],\n"
        "                    title=n[\"title\"],\n"
        "                    buyer=n[\"buyer\"],\n"
        "                    url=n[\"url\"],\n"
        "                    deadline_str=n[\"deadline_str\"],\n"
        "                    raw_summary=n[\"raw_summary\"],\n"
        "                    is_mock=is_mock_run,\n"
        "                    is_unreviewed=True,\n"
        "                )\n"
        "            )\n"
        "            saved_unreviewed += 1\n"
        "        await db.commit()\n\n"
        "    scored = 0\n    unscored = 0\n",
        "main save block")
    t = sub(t,
        "            continue  # already scored on an earlier run\n",
        "            continue  # already scored on an earlier run\n\n"
        "        if tender is not None and tender.is_unreviewed:\n"
        "            tender.is_unreviewed = False  # now passes the keyword filter, so it is being scored\n",
        "main clear flag")
    t = sub(t,
        "    response.headers[\"X-Stopped-Reason\"] = stop_reason or \"none\"\n",
        "    response.headers[\"X-Stopped-Reason\"] = stop_reason or \"none\"\n"
        "    response.headers[\"X-Unreviewed\"] = str(saved_unreviewed)\n",
        "main header")
    return t


TARGETS = [
    ("app/models.py", patch_models, "is_unreviewed"),
    ("app/collectors/ungm.py", patch_ungm, "MAX_UNREVIEWED"),
    ("main.py", patch_main, "saved_unreviewed"),
]

new_contents = {}
for rel, fn, marker in TARGETS:
    p = ROOT / rel
    if not p.exists():
        sys.exit(f"ABORTED: {rel} not found. Run this from the project root.")
    text = p.read_text(encoding="utf-8")
    if marker in text:
        sys.exit(f"ABORTED: {rel} already contains '{marker}' - patch looks already applied.")
    new_contents[p] = fn(text)

for p, text in new_contents.items():
    shutil.copy2(p, str(p) + ".bak-unreviewed")
    p.write_text(text, encoding="utf-8")
    print(f"patched {p.relative_to(ROOT)}  (backup: {p.name}.bak-unreviewed)")

print("\nNext: python migrate_add_columns.py   then restart uvicorn.")
