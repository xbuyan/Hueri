"""
Phase 1: find out what UNGM's page actually loads over the network.

Run this once. It opens the notice page in a real (headless) browser,
lets it fully load, and records every response whose Content-Type looks
like JSON (plus any HTML fragment responses, which some sites use for
server-side-rendered AJAX instead of JSON). Each captured response is
written to discovered/, and a summary table is printed so you can spot
the notices list at a glance.

Why this instead of guessing selectors: a JS-rendered page's real data
almost always arrives as a plain HTTP response you can call directly and
much faster than rendering a browser on every run. Finding it is a
one-time investigation; skipping it is why the first version silently
returned 0 notices.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from collector.config import UNGM_NOTICE_PAGE, TIMEOUT_MS, DISCOVERED_DIR, USER_AGENT

JSONISH = re.compile(r"json", re.I)


def safe_filename(url: str, idx: int) -> str:
    stem = re.sub(r"[^a-zA-Z0-9]+", "_", url)[-80:]
    return f"{idx:03d}_{stem}.json"


def main() -> int:
    out_dir = Path(DISCOVERED_DIR)
    out_dir.mkdir(exist_ok=True)

    captured: list[dict] = []

    print(f"Launching headless browser -> {UNGM_NOTICE_PAGE}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        def on_response(response):
            ctype = response.headers.get("content-type", "")
            if not JSONISH.search(ctype):
                return
            try:
                body = response.text()
            except Exception:
                return
            captured.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "content_type": ctype,
                    "body": body,
                }
            )

        page.on("response", on_response)

        try:
            page.goto(UNGM_NOTICE_PAGE, wait_until="networkidle", timeout=TIMEOUT_MS)
        except PWTimeout:
            print(
                "Page didn't reach 'networkidle' in time — it may keep polling in "
                "the background. Continuing with whatever was captured so far.",
                file=sys.stderr,
            )

        # Give any late XHRs (e.g. a search request fired on scroll) a moment.
        page.wait_for_timeout(3000)

        final_html_len = len(page.content())
        browser.close()

    print(f"\nFinal rendered HTML length: {final_html_len} chars")
    print(f"Captured {len(captured)} JSON-ish responses.\n")

    if not captured:
        print(
            "No JSON responses captured. Either the page truly renders "
            "server-side (check discovered HTML manually) or it needs a "
            "logged-in session / different wait condition. Try increasing "
            "TIMEOUT_MS in collector/config.py, or inspect manually with "
            "your browser's DevTools > Network tab, filtered to Fetch/XHR."
        )
        return 1

    print(f"{'#':<4}{'status':<8}{'size':<10}{'url'}")
    for i, item in enumerate(captured):
        fname = safe_filename(item["url"], i)
        (out_dir / fname).write_text(item["body"], encoding="utf-8")
        preview_len = len(item["body"])
        print(f"{i:<4}{item['status']:<8}{preview_len:<10}{item['url']}")

    print(f"\nFull bodies saved under {out_dir}/")
    print(
        "Open the ones that look list-shaped (array of objects, or an "
        "object with a data/items/results array) and check the field "
        "names against collector/models.py's TenderNotice. Put that URL "
        "into UNGM_API_URL in collector/config.py."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
