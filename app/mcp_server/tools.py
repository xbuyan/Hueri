"""The three read-only tool bodies, independent of the MCP SDK.

Each function takes an AsyncSession (SELECT-only role) and returns a plain dict:
{"ok": True, ...} or {"ok": False, "error": {"code": ..., "message": ...}}.
The SDK wiring layer decides how to present errors to the client.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.exc import SQLAlchemyError

from . import repository as repo
from . import serialize as ser

log = logging.getLogger("hueri.mcp.tools")


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _internal_error() -> Dict[str, Any]:
    return _error("internal_error", "The tender database is unavailable. Try again later.")


def _valid_id(tender_id: Any) -> Optional[int]:
    if isinstance(tender_id, bool) or not isinstance(tender_id, int):
        return None
    return tender_id if 1 <= tender_id <= repo.MAX_TENDER_ID else None


async def search_tenders(
    session,
    *,
    query: Any = None,
    source: Any = None,
    min_score: Any = None,
    fit_only: Any = False,
    analysis: Any = "any",
    include_mock: Any = False,
    include_unreviewed: Any = False,
    sort: Any = "best_match",
    limit: Any = repo.DEFAULT_LIMIT,
    cursor: Any = None,
) -> Dict[str, Any]:
    try:
        params = repo.build_params(
            query=query, source=source, min_score=min_score, fit_only=fit_only,
            analysis=analysis, include_mock=include_mock,
            include_unreviewed=include_unreviewed, sort=sort, limit=limit,
        )
        signature = repo.filter_signature(params)
        if cursor is not None:
            offset = repo.decode_cursor(cursor, signature)
            params = repo.build_params(
                query=query, source=source, min_score=min_score, fit_only=fit_only,
                analysis=analysis, include_mock=include_mock,
                include_unreviewed=include_unreviewed, sort=sort, limit=limit,
                offset=offset,
            )
    except repo.InvalidArguments as exc:
        return _error("invalid_arguments", str(exc))
    except repo.InvalidCursor as exc:
        return _error("invalid_cursor", str(exc))

    try:
        rows, has_more = await repo.search(session, params)
    except SQLAlchemyError:
        log.exception("search_tenders failed")
        return _internal_error()

    next_offset = params.offset + params.limit
    next_cursor = (
        repo.encode_cursor(next_offset, signature)
        if has_more and next_offset <= repo.MAX_OFFSET
        else None
    )
    return {
        "ok": True,
        "count": len(rows),
        "tenders": [ser.tender_summary(t, a) for t, a in rows],
        "next_cursor": next_cursor,
        "notice": ser.NOTICE,
    }


async def get_tender_details(session, tender_id: Any) -> Dict[str, Any]:
    valid = _valid_id(tender_id)
    if valid is None:
        return _error("invalid_arguments", "tender_id must be a positive integer")
    try:
        row = await repo.fetch_one(session, valid)
    except SQLAlchemyError:
        log.exception("get_tender_details failed")
        return _internal_error()
    if row is None:
        return _error("not_found", f"No tender with id {valid}")
    tender, analysis = row
    return {"ok": True, "tender": ser.tender_detail(tender, analysis), "notice": ser.NOTICE}


async def explain_tender_match(session, tender_id: Any) -> Dict[str, Any]:
    valid = _valid_id(tender_id)
    if valid is None:
        return _error("invalid_arguments", "tender_id must be a positive integer")
    try:
        row = await repo.fetch_one(session, valid)
    except SQLAlchemyError:
        log.exception("explain_tender_match failed")
        return _internal_error()
    if row is None:
        return _error("not_found", f"No tender with id {valid}")
    tender, analysis = row

    state = ser.analysis_state(tender, analysis)
    assessment = ser.analysis_full(analysis)
    if state == "analyzed":
        score = assessment["relevance_score"]
        score_text = "unavailable" if score is None else f"{score:.1f}/10"
        explanation = (
            f"Recorded AI assessment from the Hueri pipeline, not independently verified. "
            f"Relevance score {score_text}; the pipeline marked this tender as "
            f"{'a fit' if assessment['is_fit'] else 'not a fit'}."
        )
    elif state == "unreviewed":
        explanation = (
            "No AI analysis is recorded. This tender was saved without analysis because "
            "it did not pass keyword screening (is_unreviewed)."
        )
    else:
        explanation = (
            "No AI analysis is recorded. It may be awaiting scoring or scoring may have "
            "failed; the database cannot tell which."
        )
    if tender.is_mock:
        explanation += " This is a mock/sample record, not a real tender."

    return {
        "ok": True,
        "tender_id": tender.id,
        "title": ser.sanitize_text(tender.title, ser.TITLE_MAX),
        "analysis_state": state,
        "is_mock": bool(tender.is_mock),
        "is_unreviewed": bool(tender.is_unreviewed),
        "assessment": assessment,
        "explanation": explanation,
        "not_recorded": list(ser.NOT_RECORDED),
        "notice": ser.NOTICE,
    }
