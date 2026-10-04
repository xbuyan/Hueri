"""World Bank procurement-notices collector — same filter pipeline as UNGM's
collector, built on the shared RateLimitedCollector base.

Source: https://search.worldbank.org/api/v2/procnotices — a public,
unauthenticated JSON REST endpoint (confirmed live 2026-09-27: no login, no
CSRF token, no cookies needed, unlike UNGM). It returns a flat JSON object
with a "procnotices" array; each item is one notice.

--- Params confirmed live 2026-09-27, via direct trial against the API ---
qterm                    real keyword filter (a nonsense term returns 0 rows).
project_ctry_name_exact  real, exact-match country filter (verified: total
                          dropped from 1498 to 64 for qterm=ESIA, and every
                          returned row's project_ctry_name was "Kenya").
                          Several plausible-looking alternatives —
                          countryname_exact, countryshortname,
                          countrycode_exact — were tried and confirmed to be
                          silently ignored (same total, unfiltered rows) by
                          the API rather than erroring, so a wrong guess
                          fails quietly. Passing a list to
                          project_ctry_name_exact does NOT OR-combine values
                          (also verified live) — it's single-value only, so
                          one request per (term, country) is required; no
                          way to ask for Kenya+Uganda+Tanzania in one call.
qterm and project_ctry_name_exact combine with AND, not OR.
---------------------------------------------------------------------

Field mapping (WB JSON key -> internal notice dict), confirmed from live
sample rows:
  id                      -> external_id ("WB-{id}")
  bid_description         -> title (fallback: project_name)
  project_name            -> used in raw_summary; buyer fallback
  contact_organization    -> buyer (fallback: project_name)
  project_ctry_name       -> country
  submission_deadline_date-> deadline_str (ISO 8601; NOT present on award
                             notices, which have no open deadline — those
                             are kept, not dropped, same as an unparseable
                             UNGM deadline)
  noticedate              -> published_str (DD-Mon-YYYY, same format UNGM
                             uses, so the same _parse_date regex works)
  notice_type, bid_reference_no -> folded into raw_summary
Detail page URL pattern (confirmed via citation lookup, not the API itself):
  https://projects.worldbank.org/en/projects-operations/procurement-detail/{id}
"""
import html
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.config import settings
from app.collectors.base import RateLimitedCollector, CollectorError

logger = logging.getLogger("collectors.worldbank")

SEARCH_URL = "https://search.worldbank.org/api/v2/procnotices"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Search tuning — mirrors ungm.py so both sources are scored under the same
# relevance bar. Kept as separate constants (not imported from ungm.py) so
# the two collectors can be tuned independently later without coupling them.
# ---------------------------------------------------------------------------
SEARCH_TERMS = [
    "ESIA", "EIA", "impact assessment", "audit", "resettlement", "evaluation", "feasibility",
]

_EIA_CONTEXT = (
    r"(?:environmental|consultanc|stud(?:y|ies)|report|review|assessment|"
    r"screening|scoping|service|expert|licen[cs]e|conduct|undertak|perform|"
    r"prepar|guideline)"
)

RELEVANCE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\besia\b",
        rf"\beia\b.{{0,40}}{_EIA_CONTEXT}",
        rf"{_EIA_CONTEXT}.{{0,40}}\beia\b",
        r"\(\s*eia\s*\)",
        r"impact assessment", r"\baudit",
        r"resettlement", r"\bevaluat", r"feasibility",
    )
]

# Exact project_ctry_name values queried server-side, one request series per
# value (the API takes a single value; lists are not OR-combined). The two
# regional labels were seen verbatim in live API data (a Kenya/Uganda/Tanzania
# search alone would silently miss notices filed under them).
TARGET_COUNTRY_VALUES = [
    "Kenya", "Uganda", "Tanzania",
    "Eastern and Southern Africa",
    "Horn of Africa",
]
# Client-side backstop, derived from the same list so the two can't drift apart.
TARGET_COUNTRIES = tuple(c.lower() for c in TARGET_COUNTRY_VALUES)

MIN_DAYS_TO_DEADLINE = 5
# Live finding 2026-09-28: 404 of 506 filtered notices were Contract Awards
# (already awarded, not biddable) and only 2 of 506 carried a deadline, even
# among Requests for Expression of Interest. So: drop awards outright, and
# treat a notice with no deadline as stale once it is older than this.
EXCLUDED_NOTICE_TYPES = ("contract award",)
MAX_AGE_DAYS_NO_DEADLINE = 60
PAGE_SIZE_PER_TERM = 50    # WB API allows up to 100/page per third-party docs; start
                            # conservative and verified, raise once confirmed live.
MAX_CANDIDATES = 40
MAX_UNREVIEWED = 100
# notice_text is the full notice body (HTML) — real scope-of-work text, far
# more useful to Gemini than a bare title. Trimmed and tag-stripped, folded
# into raw_summary so the existing relevance regex also benefits from it.
MAX_NOTICE_TEXT_CHARS = 1500

# Server-side param names, both confirmed live (see module docstring).
QTERM_PARAM = "qterm"
COUNTRY_PARAM = "project_ctry_name_exact"

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_notice_text(raw: Optional[str]) -> str:
    """Strip HTML tags/entities from notice_text and cap its length."""
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = " ".join(text.split())
    return text[:MAX_NOTICE_TEXT_CHARS]


def _parse_published_date(text: Optional[str]) -> Optional[date]:
    """'26-Sep-2026' -> date(2026, 9, 26). Same format as UNGM's published/deadline text."""
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


def _parse_iso_date(text: Optional[str]) -> Optional[date]:
    """'2026-10-15T00:00:00Z' -> date(2026, 10, 15). None if unparseable/missing."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class WorldBankCollector(RateLimitedCollector):
    SOURCE_NAME = "World Bank"
    MAX_RETRIES = 3
    REQUEST_DELAY_SECONDS = 2.0
    MAX_PAGES_PER_TERM = 3
    RATE_LIMIT_COOLDOWN_MINUTES = 30

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        # Set by collect() before each per-country search_terms() run, so the
        # base class's (term, page) hook needs no signature change.
        self._current_country: Optional[str] = None

    def _do_request(self, term: str, page_size: int, page_index: int) -> List[Dict[str, Any]]:
        """One GET for one (term, current country, page). No auth/token needed."""
        params = {
            "format": "json",
            "rows": page_size,
            "os": page_index * page_size,  # WB paginates via offset, not page number
            QTERM_PARAM: term,
        }
        if self._current_country:
            params[COUNTRY_PARAM] = self._current_country
        response = self.session.get(
            SEARCH_URL, params=params, timeout=getattr(settings, "REQUEST_TIMEOUT_S", 20)
        )
        response.raise_for_status()
        return self._parse_response(response)

    def _parse_response(self, response: requests.Response) -> List[Dict[str, Any]]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectorError(
                f"World Bank response didn't parse as JSON. First 300 chars: {response.text[:300]}"
            ) from exc

        items = payload.get("procnotices")
        if items is None:
            logger.error(
                "Unrecognized JSON response shape from World Bank. Top-level keys: %s.",
                list(payload.keys()) if isinstance(payload, dict) else type(payload),
            )
            raise CollectorError("World Bank JSON response shape not recognized (no 'procnotices' key).")

        notices = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            title = item.get("bid_description") or item.get("project_name") or "(no title)"
            buyer = item.get("contact_organization") or item.get("project_name")
            country = item.get("project_ctry_name")
            deadline = item.get("submission_deadline_date")  # ISO or absent (e.g. awards)
            notice_text = _clean_notice_text(item.get("notice_text"))

            contact_bits = [b for b in (
                item.get("contact_name"), item.get("contact_organization"), item.get("contact_email"),
            ) if b]

            summary_parts = [p for p in (
                f"Notice type: {item.get('notice_type')}" if item.get("notice_type") else None,
                f"Reference: {item.get('bid_reference_no')}" if item.get("bid_reference_no") else None,
                f"Project: {item.get('project_name')}" if item.get("project_name") else None,
                f"Country: {country}" if country else None,
                f"Contact: {', '.join(contact_bits)}" if contact_bits else None,
                notice_text or None,
            ) if p]

            notices.append(
                {
                    "external_id": f"WB-{item['id']}",
                    "source": "World Bank",
                    "title": title,
                    "buyer": buyer,
                    "url": f"https://projects.worldbank.org/en/projects-operations/procurement-detail/{item['id']}",
                    "deadline_str": deadline or "N/A",
                    "raw_summary": " | ".join(summary_parts),
                    "published_str": item.get("noticedate"),
                    "country": country,
                    "notice_type": item.get("notice_type"),
                }
            )
        return notices

    @staticmethod
    def _reject_reason(notice: Dict[str, Any], earliest_deadline: date) -> Optional[str]:
        """Same three-stage filter as UNGM: deadline, then country, then title relevance."""
        if (notice.get("notice_type") or "").strip().lower() in EXCLUDED_NOTICE_TYPES:
            return "award"  # already awarded: nothing to bid on

        deadline_raw = notice.get("deadline_str")
        deadline = _parse_iso_date(deadline_raw) if deadline_raw != "N/A" else None
        if deadline is not None and deadline < earliest_deadline:
            return "deadline"
        if deadline is None:
            # No usable deadline: keep only if recently published. An
            # unparseable publish date is kept rather than silently dropped.
            published = _parse_published_date(notice.get("published_str"))
            oldest_ok = datetime.now(timezone.utc).date() - timedelta(days=MAX_AGE_DAYS_NO_DEADLINE)
            if published is not None and published < oldest_ok:
                return "stale"

        country = (notice.get("country") or "").lower()
        text = f"{notice.get('title') or ''} {notice.get('raw_summary') or ''}".lower()
        if not any(c in country or c in text for c in TARGET_COUNTRIES):
            return "country"

        title = notice.get("title") or ""
        if not any(p.search(title) for p in RELEVANCE_PATTERNS):
            return "relevance"
        return None

    def collect(
        self, page_size: int = PAGE_SIZE_PER_TERM
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Same contract as UNGMCollector.collect(): (candidates, unreviewed).
        See ungm.py's collect() docstring for the full behavior description —
        deliberately identical here so main.py can treat both sources the same way.
        """
        merged: Dict[str, Dict[str, Any]] = {}
        failed_terms: List[str] = []
        aborted = False
        for country in TARGET_COUNTRY_VALUES:
            self._current_country = country
            try:
                got, failed, was_aborted = self.search_terms(SEARCH_TERMS, page_size)
            finally:
                self._current_country = None
            for ext_id, notice in got.items():
                merged.setdefault(ext_id, notice)
            failed_terms.extend(f"{t} ({country})" for t in failed)
            if was_aborted:  # 429/4xx: stop contacting World Bank for the rest of the run
                aborted = True
                break
        total_searches = len(SEARCH_TERMS) * len(TARGET_COUNTRY_VALUES)

        if not merged and (aborted or len(failed_terms) == total_searches):
            raise CollectorError(
                "World Bank live collection failed: stopped early (rate limit or rejected "
                "request) or every search failed."
            )
        if aborted:
            logger.warning("World Bank collection stopped early; results are partial.")
        if failed_terms and not aborted:
            logger.warning(
                "Collected with %d of %d searches failing (%s); results may be incomplete.",
                len(failed_terms), total_searches, ", ".join(failed_terms),
            )

        earliest_deadline = datetime.now(timezone.utc).date() + timedelta(days=MIN_DAYS_TO_DEADLINE)
        kept: List[Dict[str, Any]] = []
        unreviewed: List[Dict[str, Any]] = []
        dropped = {"award": 0, "deadline": 0, "stale": 0, "country": 0, "relevance": 0}
        for notice in merged.values():
            reason = self._reject_reason(notice, earliest_deadline)
            if reason:
                dropped[reason] += 1
                if reason == "relevance":
                    unreviewed.append(notice)
            else:
                kept.append(notice)

        newest = lambda n: _parse_published_date(n.get("published_str")) or date.min
        kept.sort(key=newest, reverse=True)
        unreviewed.sort(key=newest, reverse=True)
        kept = kept[:MAX_CANDIDATES]
        unreviewed = unreviewed[:MAX_UNREVIEWED]

        logger.info(
            "World Bank: %d unique notices -> %d candidates, %d unreviewed "
            "(dropped: %d award, %d deadline, %d stale, %d country, %d relevance)",
            len(merged), len(kept), len(unreviewed),
            dropped["award"], dropped["deadline"], dropped["stale"],
            dropped["country"], dropped["relevance"],
        )
        return kept, unreviewed


worldbank_collector = WorldBankCollector()


if __name__ == "__main__":
    # Preview only — no DB writes, no Gemini calls.
    #     python -m app.collectors.worldbank
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    found, unreviewed = worldbank_collector.collect()
    print(f"\n{len(found)} candidate tenders (newest published first):\n")
    for n in found:
        print(
            f"{(n.get('published_str') or '?'):>11} | due {str(n['deadline_str'])[:10]:<10} | "
            f"{(n.get('country') or '?')[:18]:<18} | {n['title'][:80]}"
        )
    print(f"\n{len(unreviewed)} unreviewed (passed country/deadline, failed keyword match):\n")
    for n in unreviewed[:25]:
        print(
            f"{(n.get('published_str') or '?'):>11} | {(n.get('country') or '?')[:18]:<18} | {n['title'][:90]}"
        )
