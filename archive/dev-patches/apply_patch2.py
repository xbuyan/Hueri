#!/usr/bin/env python3
"""Patch 2: make the UNGM collector safe against rate limits, and paginate.

Run from the project root (venv active):  python apply_patch2.py
Requires patch 1 (the 'unreviewed' bucket) to be applied already.
All-or-nothing: every edit is anchored to exact text; if any anchor fails,
nothing is written. Backup: app/collectors/ungm.py.bak-patch2
Does NOT contact UNGM.
"""
import re
import shutil
import sys
from pathlib import Path

P = Path("app/collectors/ungm.py")
if not P.exists():
    sys.exit("ABORTED: app/collectors/ungm.py not found. Run from the project root.")
t = P.read_text(encoding="utf-8")
if "MAX_PAGES_PER_TERM" in t:
    sys.exit("ABORTED: patch 2 looks already applied.")
if "def collect(" not in t:
    sys.exit("ABORTED: patch 1 (unreviewed bucket) is not applied yet.")


def sub(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f"ABORTED: anchor '{label}' matched {n} times (expected 1). Nothing was changed.")
    return text.replace(old, new, 1)


# 1. Force the known-good page size (UNGM answers 400 to 25 and 50).
t, n = re.subn(r"(?m)^PAGE_SIZE_PER_TERM = \d+", "PAGE_SIZE_PER_TERM = 15", t)
if n != 1:
    sys.exit("ABORTED: could not find the PAGE_SIZE_PER_TERM line.")

# 2. Constants + shared rate-limit state.
t = sub(t,
    "TERM_DELAY_SECONDS = 1.0   # politeness delay between UNGM searches\n",
    "TERM_DELAY_SECONDS = 2.0   # politeness delay between UNGM requests\n"
    "MAX_PAGES_PER_TERM = 3     # follow PageIndex while pages come back full, up to this many\n"
    "RATE_LIMIT_COOLDOWN_MINUTES = 30  # after a 429, don't contact UNGM again for this long\n"
    "\n"
    "# Process-local. Resets when uvicorn reloads/restarts or in a fresh `python -m` run.\n"
    "_STATE: Dict[str, Optional[datetime]] = {\"rate_limited_until\": None}\n",
    "constants")

# 3. New exception type that stops the whole run.
t = sub(t,
    '''class UNGMCollectorError(Exception):
    """Raised when live collection fails after retries. Never swallowed into mock data."""
''',
    '''class UNGMCollectorError(Exception):
    """Raised when live collection fails after retries. Never swallowed into mock data."""


class UNGMRunAborted(UNGMCollectorError):
    """UNGM rate-limited us (429) or rejected a request (other 4xx). Retrying cannot help,
    so the whole run stops instead of hammering UNGM with the remaining searches."""
''',
    "exceptions")

# 4. Page index support in the payload.
t = sub(t,
    '    def _build_payload(self, page_size: int, term: str = "") -> Dict[str, Any]:\n',
    '    def _build_payload(self, page_size: int, term: str = "", page_index: int = 0) -> Dict[str, Any]:\n',
    "payload signature")
t = sub(t, '            "PageIndex": 0,\n', '            "PageIndex": page_index,\n', "payload PageIndex")

# 5. _search_term: take page_index, and fail fast on 4xx.
t = sub(t,
    "    def _search_term(self, term: str, page_size: int, token: Optional[str]):\n",
    "    def _search_term(self, term: str, page_size: int, token: Optional[str], page_index: int = 0):\n",
    "search_term signature")
t = sub(t,
    "                    json=self._build_payload(page_size, term),\n",
    "                    json=self._build_payload(page_size, term, page_index),\n",
    "search_term payload call")
t = sub(t,
    "            except Exception as exc:  # noqa: BLE001\n                last_exc = exc\n",
    "            except Exception as exc:  # noqa: BLE001\n"
    "                status_code = getattr(getattr(exc, \"response\", None), \"status_code\", None)\n"
    "                if status_code == 429:\n"
    "                    _STATE[\"rate_limited_until\"] = datetime.now(timezone.utc) + timedelta(\n"
    "                        minutes=RATE_LIMIT_COOLDOWN_MINUTES\n"
    "                    )\n"
    "                    raise UNGMRunAborted(\n"
    "                        f\"UNGM returned 429 Too Many Requests; pausing all UNGM requests for \"\n"
    "                        f\"{RATE_LIMIT_COOLDOWN_MINUTES} minutes.\"\n"
    "                    ) from exc\n"
    "                if status_code is not None and 400 <= status_code < 500:\n"
    "                    raise UNGMRunAborted(\n"
    "                        f\"UNGM rejected the request for '{term}' (page {page_index}) with HTTP \"\n"
    "                        f\"{status_code}; not retrying.\"\n"
    "                    ) from exc\n"
    "                last_exc = exc\n",
    "search_term except")

# 6. Paged wrapper (inserted before _reject_reason).
t = sub(t,
    "    @staticmethod\n    def _reject_reason(",
    '''    def _search_term_paged(self, term: str, page_size: int, token: Optional[str]):
        """Fetch up to MAX_PAGES_PER_TERM pages for one term; stop at the first short page."""
        collected: List[Dict[str, Any]] = []
        for page in range(MAX_PAGES_PER_TERM):
            if page:
                time.sleep(TERM_DELAY_SECONDS)
            try:
                notices, token = self._search_term(term, page_size, token, page_index=page)
            except UNGMRunAborted:
                raise
            except UNGMCollectorError as exc:
                if page == 0:
                    raise
                logger.warning("UNGM '%s' page %d failed, keeping earlier pages: %s", term, page + 1, exc)
                break
            collected.extend(notices)
            if len(notices) < page_size:
                break
        else:
            logger.info(
                "UNGM '%s': all %d pages were full; older results were not fetched.",
                term, MAX_PAGES_PER_TERM,
            )
        return collected, token

    @staticmethod
    def _reject_reason(''',
    "paged method")

# 7. collect(): cooldown guard, use paged search, stop early on abort.
t = sub(t,
    "        token: Optional[str] = None\n        merged: Dict[str, Dict[str, Any]] = {}\n",
    "        until = _STATE[\"rate_limited_until\"]\n"
    "        if until and datetime.now(timezone.utc) < until:\n"
    "            raise UNGMCollectorError(\n"
    "                f\"UNGM rate-limit cooldown active until {until:%H:%M} UTC; not contacting UNGM.\"\n"
    "            )\n\n"
    "        token: Optional[str] = None\n        merged: Dict[str, Dict[str, Any]] = {}\n",
    "cooldown guard")
t = sub(t, "        raw_rows = 0\n", "        raw_rows = 0\n        aborted = False\n", "aborted flag")
t = sub(t,
    "            try:\n"
    "                notices, token = self._search_term(term, page_size, token)\n"
    "            except UNGMCollectorError as exc:\n",
    "            try:\n"
    "                notices, token = self._search_term_paged(term, page_size, token)\n"
    "            except UNGMRunAborted as exc:\n"
    "                logger.error(\"Stopping UNGM collection early: %s\", exc)\n"
    "                aborted = True\n"
    "                break\n"
    "            except UNGMCollectorError as exc:\n",
    "collect loop")
t = sub(t,
    "        if len(failed_terms) == len(SEARCH_TERMS):\n"
    "            raise UNGMCollectorError(\n"
    "                f\"UNGM live collection failed: all {len(SEARCH_TERMS)} searches failed.\"\n"
    "            )\n",
    "        if not merged and (aborted or len(failed_terms) == len(SEARCH_TERMS)):\n"
    "            raise UNGMCollectorError(\n"
    "                \"UNGM live collection failed: stopped early (rate limit or rejected request) \"\n"
    "                \"or every search failed.\"\n"
    "            )\n"
    "        if aborted:\n"
    "            logger.warning(\"UNGM collection stopped early; results are partial.\")\n",
    "all-failed check")

shutil.copy2(P, str(P) + ".bak-patch2")
P.write_text(t, encoding="utf-8")
print("patched app/collectors/ungm.py  (backup: ungm.py.bak-patch2)")
print("PAGE_SIZE_PER_TERM forced to 15. No requests were sent to UNGM.")
