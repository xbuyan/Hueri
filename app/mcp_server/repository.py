"""Read-only data access for the MCP tools.

Only SELECT statements are built here, and only app.models is imported from the
application. The caller supplies an AsyncSession that should be connected with
the SELECT-only database role. Relationships are never lazy-loaded; analyses are
fetched with an explicit outer join (TenderAnalysis.tender_id is unique, so the
relationship is one-to-one).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, List, Optional, Tuple

from sqlalchemy import or_, select

from app.models import Tender, TenderAnalysis

SORTS = ("best_match", "newest")
ANALYSIS_STATES = ("any", "analyzed", "unanalyzed")
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
MAX_OFFSET = 1000
MAX_TEXT = 100
MAX_CURSOR_LENGTH = 200
MAX_TENDER_ID = 2**31 - 1


class InvalidArguments(ValueError):
    pass


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class SearchParams:
    query: Optional[str] = None
    source: Optional[str] = None
    min_score: Optional[float] = None
    fit_only: bool = False
    analysis: str = "any"
    include_mock: bool = False
    include_unreviewed: bool = False
    sort: str = "best_match"
    limit: int = DEFAULT_LIMIT
    offset: int = 0


def _text(name: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidArguments(f"{name} must be a string")
    value = value.strip()
    if len(value) > MAX_TEXT:
        raise InvalidArguments(f"{name} must be at most {MAX_TEXT} characters")
    return value or None


def _flag(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise InvalidArguments(f"{name} must be true or false")
    return value


def build_params(
    *,
    query: Any = None,
    source: Any = None,
    min_score: Any = None,
    fit_only: Any = False,
    analysis: Any = "any",
    include_mock: Any = False,
    include_unreviewed: Any = False,
    sort: Any = "best_match",
    limit: Any = DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchParams:
    if min_score is not None:
        if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
            raise InvalidArguments("min_score must be a number")
        if not math.isfinite(min_score) or not 0 <= min_score <= 10:
            raise InvalidArguments("min_score must be between 0 and 10")
        min_score = float(min_score)
    if analysis not in ANALYSIS_STATES:
        raise InvalidArguments(f"analysis must be one of {', '.join(ANALYSIS_STATES)}")
    if sort not in SORTS:
        raise InvalidArguments(f"sort must be one of {', '.join(SORTS)}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise InvalidArguments(f"limit must be an integer between 1 and {MAX_LIMIT}")
    fit_only = _flag("fit_only", fit_only)
    if analysis == "unanalyzed" and (min_score is not None or fit_only):
        raise InvalidArguments(
            "min_score and fit_only need an analysis, so they cannot be combined "
            "with analysis=unanalyzed"
        )
    return SearchParams(
        query=_text("query", query),
        source=_text("source", source),
        min_score=min_score,
        fit_only=fit_only,
        analysis=analysis,
        include_mock=_flag("include_mock", include_mock),
        include_unreviewed=_flag("include_unreviewed", include_unreviewed),
        sort=sort,
        limit=limit,
        offset=offset,
    )


def filter_signature(params: SearchParams) -> str:
    data = {k: v for k, v in asdict(params).items() if k not in ("limit", "offset")}
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def encode_cursor(offset: int, signature: str) -> str:
    raw = json.dumps({"v": 1, "o": offset, "s": signature}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: Any, signature: str) -> int:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        offset = data["o"]
        if (
            data["v"] != 1
            or data["s"] != signature
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 < offset <= MAX_OFFSET
        ):
            raise ValueError
        return offset
    except Exception:
        raise InvalidCursor("cursor is invalid or does not match these filters") from None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_search_stmt(p: SearchParams):
    stmt = select(Tender, TenderAnalysis).outerjoin(
        TenderAnalysis, TenderAnalysis.tender_id == Tender.id
    )
    if not p.include_mock:
        stmt = stmt.where(Tender.is_mock.is_(False))
    if not p.include_unreviewed:
        stmt = stmt.where(Tender.is_unreviewed.is_(False))
    if p.source:
        stmt = stmt.where(Tender.source == p.source)
    if p.query:
        pattern = f"%{_escape_like(p.query)}%"
        stmt = stmt.where(
            or_(
                Tender.title.ilike(pattern, escape="\\"),
                Tender.buyer.ilike(pattern, escape="\\"),
            )
        )
    # Unlike the REST endpoint, score/fit filters never let unanalyzed rows through.
    needs_analysis = p.analysis == "analyzed" or p.min_score is not None or p.fit_only
    if p.analysis == "unanalyzed":
        stmt = stmt.where(TenderAnalysis.id.is_(None))
    elif needs_analysis:
        stmt = stmt.where(TenderAnalysis.id.is_not(None))
    if p.min_score is not None:
        stmt = stmt.where(TenderAnalysis.relevance_score >= p.min_score)
    if p.fit_only:
        stmt = stmt.where(TenderAnalysis.is_fit.is_(True))
    if p.sort == "best_match":
        stmt = stmt.order_by(TenderAnalysis.relevance_score.desc().nulls_last(), Tender.id.desc())
    else:
        stmt = stmt.order_by(Tender.created_at.desc().nulls_last(), Tender.id.desc())
    return stmt.limit(p.limit + 1).offset(p.offset)


def build_fetch_stmt(tender_id: int):
    return (
        select(Tender, TenderAnalysis)
        .outerjoin(TenderAnalysis, TenderAnalysis.tender_id == Tender.id)
        .where(Tender.id == tender_id)
    )


async def search(session, params: SearchParams) -> Tuple[List[Tuple[Any, Any]], bool]:
    """Return (rows, has_more). Fetches limit+1 rows to learn whether more exist."""
    result = await session.execute(build_search_stmt(params))
    rows = [(row[0], row[1]) for row in result.all()]
    has_more = len(rows) > params.limit
    return rows[: params.limit], has_more


async def fetch_one(session, tender_id: int) -> Optional[Tuple[Any, Any]]:
    result = await session.execute(build_fetch_stmt(tender_id))
    row = result.first()
    return (row[0], row[1]) if row else None
