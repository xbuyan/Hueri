"""
Phase 2: the actual collector.

Design choices that make this more robust than the first attempt:

- Tries the JSON API first (config.UNGM_API_URL, found via discover_api.py)
  because it's fast and stable. Only falls back to full browser rendering
  if no API URL is configured, or the API call fails.
- Every network call is wrapped in a retry-with-backoff, because
  government/UN sites throttle or hiccup — a single failed request
  should not kill a scheduled run.
- Logs to both console and collector.log, so a cron/GitHub Actions run
  you didn't watch live still leaves a record of what happened.
- Raises loudly (raise_for_status, explicit exceptions) rather than
  silently returning an empty list, so a broken source shows up as a
  visible error instead of a quiet "0 notices" that looks like success.
- Never guesses field names silently: if the configured API's JSON shape
  doesn't match what parse_api_response() expects, it logs the raw keys
  it saw so you can fix the mapping quickly instead of debugging blind.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from collector.config import (
    UNGM_NOTICE_PAGE,
    UNGM_API_URL,
    UNGM_API_PARAMS,
    TIMEOUT_MS,
    REQUEST_TIMEOUT_S,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE_S,
    USER_AGENT,
    OUTPUT_JSONL,
    LOG_FILE,
)
from collector.models import TenderNotice

logger = logging.getLogger("ungm_collector")


def setup_logging() -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


class CollectorError(Exception):
    """Raised when a source fails after all retries — never swallowed silently."""


def with_retries(fn, *args, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything network-ish
            last_exc = exc
            wait = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
            logger.warning(
                "Attempt %d/%d failed (%s: %s) — retrying in %ds",
                attempt, MAX_RETRIES, type(exc).__name__, exc, wait,
            )
            time.sleep(wait)
    raise CollectorError(f"All {MAX_RETRIES} attempts failed: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Strategy 1: direct JSON API call (fast path, used once discover_api.py
# has told us the endpoint)
# ---------------------------------------------------------------------------

def _fetch_api_once() -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    resp = requests.get(
        UNGM_API_URL, params=UNGM_API_PARAMS, headers=headers, timeout=REQUEST_TIMEOUT_S
    )
    resp.raise_for_status()
    return resp.json()


def fetch_via_api() -> list[dict]:
    logger.info("Fetching via API: %s", UNGM_API_URL)
    payload = with_retries(_fetch_api_once)

    # APIs commonly wrap the list in "data", "items", "results", or return
    # a bare array. Try the obvious shapes before giving up.
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "notices", "value"):
            if isinstance(payload.get(key), list):
                return payload[key]
        logger.error(
            "API response was a JSON object but none of the usual list keys "
            "(data/items/results/notices/value) matched. Top-level keys seen: %s",
            list(payload.keys()),
        )
    raise CollectorError(
        "Unrecognized API response shape — inspect discovered/ and adjust "
        "fetch_via_api() to match the real field names."
    )


def parse_api_response(raw_items: list[dict]) -> list[TenderNotice]:
    """
    Map raw API fields to TenderNotice. The field names on the right of
    each .get() are guesses at common UNGM-style naming — check an actual
    captured response in discovered/ and adjust these keys to match.
    """
    notices = []
    for item in raw_items:
        try:
            notices.append(
                TenderNotice(
                    source="ungm",
                    title=item.get("title") or item.get("Title") or "(no title)",
                    url=item.get("url") or item.get("noticeUrl") or UNGM_NOTICE_PAGE,
                    buyer=item.get("agency") or item.get("Agency"),
                    deadline=item.get("deadline") or item.get("DeadlineUTC"),
                    published_date=item.get("publishedDate") or item.get("PublishedDate"),
                    notice_type=item.get("noticeType") or item.get("NoticeType"),
                    country=item.get("country") or item.get("Country"),
                    reference=str(item.get("id") or item.get("noticeId") or ""),
                    full_text=json.dumps(item, ensure_ascii=False),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping one malformed record: %s", exc)
    return notices


# ---------------------------------------------------------------------------
# Strategy 2: Playwright DOM fallback — used when no API URL is configured,
# or the API call raised. Slower, but actually executes the page's JS, so
# it sees what a human sees.
# ---------------------------------------------------------------------------

def _fetch_dom_once() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=USER_AGENT).new_page()
        try:
            page.goto(UNGM_NOTICE_PAGE, wait_until="networkidle", timeout=TIMEOUT_MS)
        except PWTimeout as exc:
            browser.close()
            raise CollectorError(f"Timed out waiting for page load: {exc}") from exc
        html = page.content()
        browser.close()
        return html


def fetch_via_dom() -> list[TenderNotice]:
    logger.info("No usable API configured — falling back to browser rendering: %s", UNGM_NOTICE_PAGE)
    html = with_retries(_fetch_dom_once)

    from bs4 import BeautifulSoup  # local import: only needed for this path

    soup = BeautifulSoup(html, "html.parser")
    # Placeholder selector — after rendering, inspect the actual DOM (e.g.
    # via discover_api.py's saved final_html_len hint, or your browser's
    # Elements tab) and replace this with the real notice-card selector.
    cards = soup.select("[class*='notice']")
    if not cards:
        logger.error(
            "Rendered page had %d chars of HTML but no elements matched "
            "the placeholder selector \"[class*='notice']\". Open a saved "
            "copy of this HTML and find the real notice card selector.",
            len(html),
        )
        return []

    notices = []
    for card in cards:
        link = card.find("a", href=True)
        if not link:
            continue
        notices.append(
            TenderNotice(
                source="ungm",
                title=link.get_text(strip=True),
                url=requests.compat.urljoin(UNGM_NOTICE_PAGE, link["href"]),
                full_text=card.get_text(" ", strip=True),
            )
        )
    return notices


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def collect(limit: int | None = None) -> list[TenderNotice]:
    if UNGM_API_URL:
        try:
            raw = fetch_via_api()
            notices = parse_api_response(raw)
        except CollectorError as exc:
            logger.warning("API strategy failed (%s) — falling back to DOM rendering.", exc)
            notices = fetch_via_dom()
    else:
        notices = fetch_via_dom()

    if limit:
        notices = notices[:limit]
    return notices


def append_jsonl(notices: list[TenderNotice]) -> None:
    with open(OUTPUT_JSONL, "a", encoding="utf-8") as f:
        for n in notices:
            f.write(json.dumps(n.to_dict(), ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect UNGM tender notices.")
    parser.add_argument("--limit", type=int, default=None, help="Max notices to fetch/print")
    args = parser.parse_args()

    setup_logging()

    try:
        notices = collect(limit=args.limit)
    except CollectorError as exc:
        logger.error("Collection failed after retries: %s", exc)
        return 1

    if not notices:
        logger.warning(
            "0 notices parsed. This is logged as a WARNING, not silent success — "
            "check collector.log and, if this is the API path, re-check the field "
            "mapping in parse_api_response() against discovered/."
        )
        return 1

    logger.info("Parsed %d notices.", len(notices))
    for n in notices:
        print(f"- [{n.source}] {n.title}  ({n.deadline or 'no deadline parsed'})")
        print(f"  {n.url}")

    append_jsonl(notices)
    logger.info("Appended %d notices to %s", len(notices), OUTPUT_JSONL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
