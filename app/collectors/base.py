"""Shared HTTP/retry/rate-limit/pagination logic for tender source collectors.

Each source (UNGM, PPIP, World Bank STEP, AfDB DACON, ...) has its own request
shape and response parser, but the failure-handling rules are the same for
all of them, because they were all learned the hard way against UNGM on
2026-09-25:

  - a 429 means stop contacting the source entirely for a cooldown period,
    instead of hammering it with the rest of the run's requests
  - any other 4xx means the request itself is wrong (bad params); retrying
    cannot help, so stop immediately instead of retrying 3x per search term
  - other errors (5xx, timeouts, connection resets) are retried with backoff,
    since those can be transient
  - a search that returns a full page is paged forward, capped, until a
    short page or the cap is hit, so results past page one aren't silently
    dropped

A subclass implements `_do_request(term, page_size, page_index)` to make one
HTTP call for one (term, page) and return the raw notices found on it. It
raises on failure the same way `requests` does (an exception with a
`.response.status_code`, e.g. via `response.raise_for_status()`), or any
other Exception for non-HTTP failures. Everything else (retries, 429/4xx
handling, cooldown, pagination, per-term delay) is handled here.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("collectors.base")


class CollectorError(Exception):
    """Raised when live collection fails after retries. Never swallowed into mock data."""


class RunAborted(CollectorError):
    """The source rate-limited us (429) or rejected a request (other 4xx).
    Retrying cannot help, so the whole run stops instead of continuing to
    hammer the source with the remaining searches."""


class RateLimitedCollector:
    """Base class for a tender-source collector with shared retry/rate-limit/pagination.

    Class attributes a subclass may override:
      SOURCE_NAME                  short name for log lines, e.g. "UNGM"
      MAX_RETRIES                  retries for non-4xx errors (default 3)
      REQUEST_DELAY_SECONDS        politeness delay between requests (default 2.0)
      MAX_PAGES_PER_TERM           pages to follow per search before giving up (default 3)
      RATE_LIMIT_COOLDOWN_MINUTES  minutes to stay off the source after a 429 (default 30)

    A subclass implements:
      _do_request(term, page_size, page_index) -> List[Dict[str, Any]]
          Make exactly one HTTP call and return the raw notices on that page
          (each notice a dict with at least an "external_id" key). Raise on
          failure the way `requests` does.
    """

    SOURCE_NAME = "source"
    MAX_RETRIES = 3
    REQUEST_DELAY_SECONDS = 2.0
    MAX_PAGES_PER_TERM = 3
    RATE_LIMIT_COOLDOWN_MINUTES = 30

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # One cooldown clock per subclass (per source), not shared across
        # sources and not shared across instances in a way that would let
        # a UNGM 429 block a PPIP run or vice versa. Process-local: resets
        # on uvicorn reload/restart or a fresh `python -m` run.
        cls._state: Dict[str, Optional[datetime]] = {"rate_limited_until": None}

    def _check_cooldown(self) -> None:
        until = self.__class__._state.get("rate_limited_until")
        if until and datetime.now(timezone.utc) < until:
            raise CollectorError(
                f"{self.SOURCE_NAME} rate-limit cooldown active until {until:%H:%M} UTC; "
                f"not contacting {self.SOURCE_NAME}."
            )

    def _start_cooldown(self) -> None:
        self.__class__._state["rate_limited_until"] = datetime.now(timezone.utc) + timedelta(
            minutes=self.RATE_LIMIT_COOLDOWN_MINUTES
        )

    def _do_request(self, term: str, page_size: int, page_index: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def _request_with_retries(self, term: str, page_size: int, page_index: int) -> List[Dict[str, Any]]:
        """One page, with retries on non-4xx errors. Raises RunAborted on 429 or other 4xx."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                return self._do_request(term, page_size, page_index)
            except Exception as exc:  # noqa: BLE001
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code == 429:
                    self._start_cooldown()
                    raise RunAborted(
                        f"{self.SOURCE_NAME} returned 429 Too Many Requests; pausing all "
                        f"{self.SOURCE_NAME} requests for {self.RATE_LIMIT_COOLDOWN_MINUTES} minutes."
                    ) from exc
                if status_code is not None and 400 <= status_code < 500:
                    raise RunAborted(
                        f"{self.SOURCE_NAME} rejected the request for '{term}' (page {page_index}) "
                        f"with HTTP {status_code}; not retrying."
                    ) from exc
                last_exc = exc
                wait = 2 ** (attempt - 1)
                logger.warning(
                    "%s search '%s' page %d attempt %d/%d failed (%s: %s) — retrying in %ds",
                    self.SOURCE_NAME, term, page_index, attempt, self.MAX_RETRIES,
                    type(exc).__name__, exc, wait,
                )
                time.sleep(wait)
        raise CollectorError(
            f"{self.SOURCE_NAME} search for '{term}' page {page_index} failed after "
            f"{self.MAX_RETRIES} attempts: {last_exc}"
        )

    def _search_term_paged(self, term: str, page_size: int) -> List[Dict[str, Any]]:
        """Fetch up to MAX_PAGES_PER_TERM pages for one term; stop at the first short page."""
        collected: List[Dict[str, Any]] = []
        for page in range(self.MAX_PAGES_PER_TERM):
            if page:
                time.sleep(self.REQUEST_DELAY_SECONDS)
            try:
                notices = self._request_with_retries(term, page_size, page)
            except RunAborted:
                raise
            except CollectorError as exc:
                if page == 0:
                    raise
                logger.warning(
                    "%s '%s' page %d failed, keeping earlier pages: %s",
                    self.SOURCE_NAME, term, page + 1, exc,
                )
                break
            collected.extend(notices)
            if len(notices) < page_size:
                break
        else:
            logger.info(
                "%s '%s': all %d pages were full; older results were not fetched.",
                self.SOURCE_NAME, term, self.MAX_PAGES_PER_TERM,
            )
        return collected

    def search_terms(
        self, terms: List[str], page_size: int
    ) -> Tuple[Dict[str, Dict[str, Any]], List[str], bool]:
        """
        Run one paged search per term, merging results by external_id (first
        occurrence wins). Returns (merged, failed_terms, aborted):
          merged        external_id -> notice, deduplicated across terms/pages
          failed_terms  terms that failed (non-abort errors) and were skipped
          aborted       True if a 429/4xx stopped the run before all terms ran

        Raises CollectorError up front if a rate-limit cooldown from an
        earlier run is still active — no request is sent in that case.
        """
        self._check_cooldown()
        merged: Dict[str, Dict[str, Any]] = {}
        failed_terms: List[str] = []
        aborted = False
        for i, term in enumerate(terms):
            if i:
                time.sleep(self.REQUEST_DELAY_SECONDS)
            try:
                notices = self._search_term_paged(term, page_size)
            except RunAborted as exc:
                logger.error("Stopping %s collection early: %s", self.SOURCE_NAME, exc)
                aborted = True
                break
            except CollectorError as exc:
                logger.warning("Skipping search term '%s': %s", term, exc)
                failed_terms.append(term)
                continue
            logger.info("%s search '%s': %d notices", self.SOURCE_NAME, term, len(notices))
            for notice in notices:
                merged.setdefault(notice["external_id"], notice)
        return merged, failed_terms, aborted
