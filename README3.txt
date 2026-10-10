HUERI patch 3: shared base class for tender-source collectors
================================================================
Extracts the retry/rate-limit/pagination logic (learned the hard way
against UNGM: fail fast on 4xx, 30-min cooldown on 429, paginate up to
3 pages, 2s delay) out of ungm.py into a new app/collectors/base.py
(RateLimitedCollector). ungm.py is rewritten to inherit from it - same
UNGM request shape, token handling, filters, and unreviewed bucket as
before, just with the source-agnostic parts moved out so the next
collector (PPIP, World Bank, AfDB) can subclass RateLimitedCollector
instead of repeating tonight's process.

Requires patch 1 (unreviewed bucket) and patch 2 (rate-limit safety)
already applied.

From ~/projects/Hueri, venv active:

  unzip -o ~/Downloads/hueri_patch3.zip -d .
  python apply_patch3.py
  python -m app.collectors.ungm 2>&1 | head -40   # preview only, no DB writes

The installer:
  - refuses to run if app/collectors/base.py already exists (already applied)
  - refuses to run if ungm.py doesn't already have patch 1 + patch 2's markers
  - checks main.py only calls ungm_collector.collect / .fetch_recent_notices
    / .get_mock_data() (the only methods this rewrite keeps) - aborts and
    changes nothing if it finds anything else
  - never contacts UNGM
  - backs up the old ungm.py as ungm.py.bak-patch3

main.py needs NO changes - collect(), fetch_recent_notices(), and
get_mock_data() all keep their exact signatures and behavior.

Tested (against a stand-in UNGM, not the live site):
  - full pagination across pages
  - immediate abort + 0 retries on a 400
  - immediate abort on 429 + 30-min cooldown blocking the next run (0 requests)
  - partial results kept when a 429 hits mid-run
  - 5xx still retried 3x per term (21 requests across the real 7 SEARCH_TERMS)
  - unreviewed bucket still separates "EIA" airport-code false positives
    from real EIA/ESIA notices
  - dedup across search terms
  - antiforgery token still refreshes on a failed request and is reused
    across pages/terms otherwise
  - fetch_recent_notices() / get_mock_data() backward-compat wrappers

Rollback: cp app/collectors/ungm.py.bak-patch3 app/collectors/ungm.py
          rm app/collectors/base.py
