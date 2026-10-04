import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from app.config import settings
from app.profile import profile
from app.collectors.base import RateLimitedCollector, CollectorError

logger = logging.getLogger("collectors.ungm")

NOTICE_PAGE_URL = "https://www.ungm.org/Public/Notice"
SEARCH_URL = "https://www.ungm.org/Public/Notice/Search"

# Confirmed via browser DevTools: the antiforgery token is embedded as a
# hidden form field in the plain GET response. Captured from a live session:
#   <input name="__RequestVerificationToken" type="hidden" value="..." />
TOKEN_PATTERN = re.compile(
    r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"'
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Search tuning. Edit these to change what gets collected, and therefore what
# spends your Gemini quota. Only notices that pass ALL the filters are returned.
# ---------------------------------------------------------------------------

# Search tuning (keywords, relevance regexes, countries, deadline margin) now comes
# from profile.yaml, so each client can have their own. One UNGM search is run per
# term (server-side Title filter) and results are merged; every extra term in the
# profile is one more request.
SEARCH_TERMS = list(profile.search.terms)

# Client-side check that the title really contains a relevant term (the reasoning
# behind each pattern is documented in profile.yaml).
RELEVANCE_PATTERNS = profile.search.compiled_patterns()

# Keep a notice only if its country (or its title/summary) mentions one of these.
TARGET_COUNTRIES = tuple(c.lower() for c in profile.search.target_countries)

MIN_DAYS_TO_DEADLINE = profile.search.min_days_to_deadline   # drop notices closing sooner than this: too late to bid
PAGE_SIZE_PER_TERM = 15    # UNGM rejects other sizes (confirmed live: 25 and 50 both 400)
MAX_CANDIDATES = 40        # cap on what is returned (and so on what can be scored)
MAX_UNREVIEWED = 100       # cap on saved-but-unscored notices per run

# Server-side sort. "DatePublished" is my best guess at UNGM's name for the
# Published column and must be verified (see README). If UNGM rejects it, use
# SORT_FIELD = "Deadline" with SORT_ASCENDING = True (the old behaviour, but now
# starting MIN_DAYS_TO_DEADLINE days out). Results are ALSO sorted newest-first
# locally, so the server sort only decides which rows make the first page.
SORT_FIELD = "Deadline"
SORT_ASCENDING = True

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")


def _parse_ungm_date(text: Optional[str]) -> Optional[date]:
    """'24-Sep-2026 03:15 (GMT -4.00)' -> date(2026, 9, 24). None if unparseable."""
    if not text:
        return None
    match = _DATE_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group(2).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


# Kept as an alias so any code (or notes) still referring to the old name
# keeps working. New code should use app.collectors.base.CollectorError.
UNGMCollectorError = CollectorError


class UNGMCollector(RateLimitedCollector):
    SOURCE_NAME = "UNGM"
    MAX_RETRIES = 3
    REQUEST_DELAY_SECONDS = 2.0
    MAX_PAGES_PER_TERM = 3
    RATE_LIMIT_COOLDOWN_MINUTES = 30

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self._last_html = ""
        self._token: Optional[str] = None

    def _get_verification_token(self) -> str:
        """
        GET the public notice page to (a) pick up session cookies UNGM sets
        (UNGM.UserPreferredLanguage, GCLB, etc — handled automatically by
        requests.Session) and (b) scrape the antiforgery token UNGM requires
        echoed back as a request header on the POST.
        """
        resp = self.session.get(NOTICE_PAGE_URL, timeout=settings.REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        match = TOKEN_PATTERN.search(resp.text)
        if not match:
            raise CollectorError(
                "Could not find __RequestVerificationToken in the notice page HTML — "
                "UNGM may have changed the page structure. First 500 chars: "
                f"{resp.text[:500]}"
            )
        return match.group(1)

    def _build_payload(self, page_size: int, term: str = "", page_index: int = 0) -> Dict[str, Any]:
        today = datetime.now(timezone.utc)
        earliest_deadline = today + timedelta(days=MIN_DAYS_TO_DEADLINE)
        # Matches the exact shape captured from UNGM's own frontend via
        # DevTools — UNGM's API appears to require every one of these keys
        # present, even when empty, rather than accepting a partial body.
        return {
            "PageIndex": page_index,
            "PageSize": page_size,
            "Title": term,
            "Description": "",
            "Reference": "",
            "PublishedFrom": "",
            "PublishedTo": today.strftime("%d-%b-%Y"),
            "DeadlineFrom": earliest_deadline.strftime("%d-%b-%Y"),
            "DeadlineTo": "",
            "Countries": [],
            "Agencies": [],
            "UNSPSCs": [],
            "NoticeTypes": [],
            "SortField": SORT_FIELD,
            "SortAscending": SORT_ASCENDING,
            "isPicker": False,
            "IsSustainable": False,
            "IsActive": True,
            "NoticeDisplayType": None,
            "NoticeSearchTotalLabelId": "noticeSearchTotal",
            "TypeOfCompetitions": [],
        }

    def _do_request(self, term: str, page_size: int, page_index: int) -> List[Dict[str, Any]]:
        """
        One UNGM POST for one (term, page). Required by RateLimitedCollector;
        retries, 429/4xx handling and pagination all live there now. The
        antiforgery token is instance state (self._token) so it survives
        across the base class's retry attempts, and is cleared on any
        failure so the next attempt fetches a fresh one — same behaviour as
        the original retry loop had inline.
        """
        if self._token is None:
            self._token = self._get_verification_token()
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://www.ungm.org",
            "Referer": NOTICE_PAGE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "requestverificationtoken": self._token,
        }
        try:
            response = self.session.post(
                SEARCH_URL,
                json=self._build_payload(page_size, term, page_index),
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
        except Exception:
            self._token = None  # force a fresh token on the next attempt
            raise
        self._last_html = response.text
        return self._parse_response(response)

    @staticmethod
    def _reject_reason(notice: Dict[str, Any], earliest_deadline: date) -> Optional[str]:
        """Return why a notice is not worth scoring, or None to keep it."""
        deadline = _parse_ungm_date(notice.get("deadline_str"))
        if deadline is not None and deadline < earliest_deadline:
            return "deadline"  # an unparseable deadline is kept, not dropped

        country = (notice.get("country") or "").lower()
        text = f"{notice.get('title') or ''} {notice.get('raw_summary') or ''}".lower()
        if not any(c in country or c in text for c in TARGET_COUNTRIES):
            return "country"

        title = notice.get("title") or ""
        if not any(p.search(title) for p in RELEVANCE_PATTERNS):
            return "relevance"
        return None

    def _save_debug_html(self) -> None:
        path = "debug_ungm_response.html"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._last_html)
        except OSError:
            pass
        logger.warning(
            "No notice rows in ANY UNGM search response. Last body saved to %s — either "
            "nothing matched today's filters or UNGM changed its markup.",
            path,
        )

    def fetch_recent_notices(self, page_size: int = PAGE_SIZE_PER_TERM) -> List[Dict[str, Any]]:
        """Backward-compatible wrapper: only the keyword-matched candidates."""
        return self.collect(page_size)[0]

    def collect(
        self, page_size: int = PAGE_SIZE_PER_TERM
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Fetch live notices from UNGM, narrowed to what HUERI can act on: one
        paged search per term in SEARCH_TERMS (deadline at least
        MIN_DAYS_TO_DEADLINE away), merged and de-duplicated, then split by
        deadline, country and title relevance into:

          candidates  passed every filter; scored by Gemini. Newest-published
                      first (capped at MAX_CANDIDATES). main.py scores them in
                      this order, so if Gemini quota runs out it is the older
                      notices that wait for the next run.
          unreviewed  passed country + deadline but failed the keyword match
                      (e.g. a bare "EIA" that turned out to be an airport
                      code). Saved with no Gemini spend so nothing is
                      silently lost; capped at MAX_UNREVIEWED.

        Raises CollectorError if collection is blocked by an active
        rate-limit cooldown, if every search term failed, or if a 429/4xx
        stopped the run before anything was collected. Never silently
        substitutes mock data. A term that fails on its own (non-abort
        error) is logged and skipped; other terms still run.
        """
        self._token = None
        merged, failed_terms, aborted = self.search_terms(SEARCH_TERMS, page_size)

        if not merged and (aborted or len(failed_terms) == len(SEARCH_TERMS)):
            raise CollectorError(
                "UNGM live collection failed: stopped early (rate limit or rejected request) "
                "or every search failed."
            )
        if aborted:
            logger.warning("UNGM collection stopped early; results are partial.")
        if failed_terms and not aborted:
            logger.warning(
                "Collected with %d of %d searches failing (%s); results may be incomplete.",
                len(failed_terms), len(SEARCH_TERMS), ", ".join(failed_terms),
            )
        if not merged:
            self._save_debug_html()

        earliest_deadline = datetime.now(timezone.utc).date() + timedelta(days=MIN_DAYS_TO_DEADLINE)
        kept: List[Dict[str, Any]] = []
        unreviewed: List[Dict[str, Any]] = []
        dropped = {"deadline": 0, "country": 0, "relevance": 0}
        for notice in merged.values():
            reason = self._reject_reason(notice, earliest_deadline)
            if reason:
                dropped[reason] += 1
                if reason == "relevance":  # passed deadline + country, failed keyword match
                    unreviewed.append(notice)
            else:
                kept.append(notice)

        # Newest published first (stable, so ties keep UNGM's own order).
        newest = lambda n: _parse_ungm_date(n.get("published_str")) or date.min
        kept.sort(key=newest, reverse=True)
        unreviewed.sort(key=newest, reverse=True)
        kept = kept[:MAX_CANDIDATES]
        unreviewed = unreviewed[:MAX_UNREVIEWED]

        logger.info(
            "UNGM: %d unique notices -> %d candidates, %d unreviewed "
            "(dropped: %d deadline, %d country, %d relevance)",
            len(merged), len(kept), len(unreviewed),
            dropped["deadline"], dropped["country"], dropped["relevance"],
        )
        return kept, unreviewed

    def _parse_response(self, response: requests.Response) -> List[Dict[str, Any]]:
        """
        UNGM's /Public/Notice/Search endpoint returns a server-rendered HTML
        fragment (not JSON), confirmed via live capture. Each notice is:

            <div role="row" data-noticeid="312363" class="tableRow dataRow notice-table">
                <div role="cell" class="tableCell resultOptions">...</div>
                <div role="cell" class="tableCell resultTitle">
                    <span class="ungm-title ungm-title--small">TITLE</span>
                    <a href="/Public/Notice/312363">...</a>
                </div>
                <div role="cell" class="tableCell resultInfo1 deadline" data-description="Deadline">
                    <span>24-Sep-2026 03:15 (GMT -4.00)</span>
                </div>
                <div role="cell" class="tableCell"><span>26-Aug-2026</span></div>  <!-- published -->
                <div role="cell" class="tableCell resultAgency"><span>UNDP</span></div>
                <div role="cell" class="tableCell"><span>Call for individual consultants</span></div>  <!-- notice type -->
                <div role="cell" class="tableCell resultInfo1" data-description="Reference"><span>UNDP-TJK-00640</span></div>
                <div role="cell" class="tableCell"><span>Tajikistan</span></div>  <!-- country -->
            </div>
        """
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            # Kept as a defensive path in case UNGM changes the endpoint to
            # return JSON later — not the confirmed live behavior today.
            return self._parse_json_response(response)
        return self._parse_html_rows(response.text)

    def _parse_html_rows(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("div", attrs={"role": "row", "data-noticeid": True})

        if not rows:
            # Normal for a narrow search or an empty page. collect() saves
            # the body and warns only if EVERY search comes back empty.
            logger.info("0 notice rows in this UNGM response (%d chars of HTML).", len(html))
            return []

        notices = []
        for row in rows:
            notice_id = row.get("data-noticeid")

            title_el = row.select_one(".resultTitle .ungm-title")
            title = title_el.get_text(strip=True) if title_el else "(no title)"

            link_el = row.select_one(".resultTitle a[href]")
            href = link_el.get("href") if link_el else None
            url = f"https://www.ungm.org{href}" if href else f"https://www.ungm.org/Public/Notice/{notice_id}"

            deadline_el = row.select_one("[data-description='Deadline'] span")
            deadline = (
                " ".join(deadline_el.get_text(" ", strip=True).split()) if deadline_el else "N/A"
            )

            agency_el = row.select_one(".resultAgency span")
            agency = agency_el.get_text(strip=True) if agency_el else None

            reference_el = row.select_one("[data-description='Reference'] span")
            reference = reference_el.get_text(strip=True) if reference_el else None

            # The published-date, notice-type, and country cells share the
            # same bare "tableCell" class with nothing else to key off —
            # they're identified by position instead (confirmed order from
            # a live sample; see docstring above).
            generic_cells = [
                c for c in row.find_all(attrs={"role": "cell"})
                if c.get("class") == ["tableCell"]
            ]
            country = generic_cells[-1].get_text(strip=True) if generic_cells else None
            notice_type = generic_cells[1].get_text(strip=True) if len(generic_cells) > 1 else None
            published = generic_cells[0].get_text(strip=True) if generic_cells else None
            if published and not _DATE_RE.search(published):
                published = None  # cell order was not what we expected

            summary_parts = [p for p in (
                f"Reference: {reference}" if reference else None,
                f"Notice type: {notice_type}" if notice_type else None,
                f"Country: {country}" if country else None,
            ) if p]

            notices.append(
                {
                    "external_id": f"UNGM-{notice_id}",
                    "source": "UNGM",
                    "title": title,
                    "buyer": agency,
                    "url": url,
                    "deadline_str": deadline,
                    "raw_summary": " | ".join(summary_parts),
                    "published_str": published,
                    "country": country,
                }
            )
        return notices

    def _parse_json_response(self, response: requests.Response) -> List[Dict[str, Any]]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectorError(
                f"Content-Type claimed JSON but body didn't parse. First 300 chars: {response.text[:300]}"
            ) from exc

        items = None
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            for key in ("Data", "data", "Items", "items", "Results", "results", "Notices"):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break

        if items is None:
            logger.error(
                "Unrecognized JSON response shape from UNGM Search. Top-level keys: %s.",
                list(payload.keys()) if isinstance(payload, dict) else type(payload),
            )
            raise CollectorError("UNGM Search JSON response shape not recognized.")

        notices = []
        for item in items:
            if not isinstance(item, dict):
                continue
            notices.append(
                {
                    "external_id": f"UNGM-{item.get('Id') or item.get('NoticeId') or item.get('id')}",
                    "source": "UNGM",
                    "title": item.get("Title") or item.get("title") or "(no title)",
                    "buyer": item.get("Agency") or item.get("AgencyName") or item.get("agency"),
                    "url": f"https://www.ungm.org/Public/Notice/{item.get('Id') or item.get('NoticeId') or ''}",
                    "deadline_str": item.get("Deadline") or item.get("DeadlineOn") or item.get("deadline") or "N/A",
                    "raw_summary": item.get("Description") or item.get("description") or "",
                }
            )
        return notices

    @staticmethod
    def get_mock_data() -> List[Dict[str, Any]]:
        """
        Explicit, opt-in mock data for local development ONLY. Never called
        automatically by collect() — main.py decides whether to use this,
        and must label the resulting tenders as mock in the response so the
        frontend can display a visible "MOCK DATA" badge.
        """
        return [
            {
                "external_id": "MOCK-UNGM-982134",
                "source": "UNGM",
                "title": "Consultancy for Environmental and Social Impact Assessment (ESIA) for Regional Water Supply Project",
                "buyer": "UNOPS Kenya",
                "url": "https://www.ungm.org/Public/Notice/982134",
                "deadline_str": "18-Oct-2026",
                "raw_summary": "Consultancy firm required to conduct full ESIA, NEMA compliance assessment, and RAP for urban infrastructure upgrade.",
            },
            {
                "external_id": "MOCK-PPIP-2026-8812",
                "source": "PPIP Kenya",
                "title": "Annual Environmental Audit and Safety Inspection for Municipal Facilities in Kisumu County",
                "buyer": "County Government of Kisumu",
                "url": "https://tenders.go.ke/notice/2026-8812",
                "deadline_str": "05-Oct-2026",
                "raw_summary": "Expression of Interest from registered Lead Environmental Experts for 2026/2027 environmental audits.",
            },
        ]


ungm_collector = UNGMCollector()


if __name__ == "__main__":
    # Preview what the collector would return. Live UNGM requests only:
    # nothing is saved and Gemini is never called.  Run from the project root:
    #     python -m app.collectors.ungm
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    found, unreviewed = ungm_collector.collect()
    print(f"\n{len(found)} candidate tenders (newest published first):\n")
    for n in found:
        print(
            f"{(n.get('published_str') or '?'):>11} | due {str(n['deadline_str'])[:11]:<11} | "
            f"{(n.get('country') or '?')[:18]:<18} | {n['title'][:80]}"
        )
    print(f"\n{len(unreviewed)} unreviewed (passed country/deadline, failed keyword match):\n")
    for n in unreviewed[:25]:
        print(
            f"{(n.get('published_str') or '?'):>11} | {(n.get('country') or '?')[:18]:<18} | {n['title'][:90]}"
        )
