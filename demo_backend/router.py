"""Deterministic router: spoken-style request -> plan of real MCP tool calls -> spoken reply.

No LLM is involved. The same input always produces the same tool calls, which keeps the
demo predictable. An optional LLM router can later produce the same `Intent` objects.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .mcp_client import McpError, timed_call, timed_list_tools

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 5

_STOP = {
    "a", "an", "the", "me", "my", "please", "find", "search", "show", "list", "get", "give",
    "tender", "tenders", "opportunity", "opportunities", "bid", "bids", "procurement",
    "for", "in", "on", "about", "of", "to", "any", "all", "some", "latest", "newest", "new",
    "recent", "best", "top", "good", "fit", "fits", "relevant", "related", "relating", "with",
    "and", "or", "that", "are", "is", "there", "what", "whats", "which", "do", "we", "have",
    "from", "worldbank", "world", "bank", "ungm", "unreviewed", "unanalyzed", "score", "above",
    "over", "least", "at", "then", "explain", "why", "match", "details", "detail", "tell",
    "first", "second", "third", "one", "it", "this", "its", "brief", "briefing", "matches",
    "morning", "catch", "up", "hello", "hi", "hey", "there", "alexa", "ask", "please",
    "open", "opening", "currently", "now", "right", "today", "available", "anything",
    "closing", "still",
}
_ORDINALS = {"first": 0, "top": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}


@dataclass
class Intent:
    name: str                      # search | details | explain | brief | help
    tender_id: Optional[int] = None
    filters: Dict[str, Any] = field(default_factory=dict)
    words: List[str] = field(default_factory=list)


@dataclass
class Turn:
    intent: str
    reply: str
    steps: List[Dict[str, Any]]
    context: Dict[str, Any]
    sample_data: bool


# ----------------------------------------------------------------------------- parsing
def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9][a-z0-9\-']*", text.lower())


def parse(utterance: str, context: Optional[Dict[str, Any]] = None) -> Intent:
    text = (utterance or "").strip().lower()
    ids = [i for i in (context or {}).get("ids", []) if isinstance(i, int)]
    words = _words(text)

    tender_id: Optional[int] = None
    m = re.search(r"(?:tender|number|no\.?|id|#)\s*#?(\d{1,9})\b", text)
    if m:
        tender_id = int(m.group(1))
    else:
        for w in words:
            if w in _ORDINALS and _ORDINALS[w] < len(ids):
                tender_id = ids[_ORDINALS[w]]
                break
        if tender_id is None and re.search(r"\b(that one|this one|it)\b", text) and ids:
            tender_id = ids[0]

    filters: Dict[str, Any] = {}
    if re.search(r"world\s*bank", text):
        filters["source"] = "worldbank"
    elif "ungm" in words:
        filters["source"] = "ungm"
    if re.search(r"\b(newest|latest|recent|new)\b", text):
        filters["sort"] = "newest"
    if re.search(r"\b(good fit|fits?|relevant|worth)\b", text):
        filters["fit_only"] = True
    m = re.search(r"(?:above|over|at least|score of|score)\s*(\d+(?:\.\d+)?)", text)
    if m and 0 <= float(m.group(1)) <= 10:
        filters["min_score"] = float(m.group(1))
    if re.search(r"\b(unreviewed|unanaly[sz]ed)\b", text):
        filters["include_unreviewed"] = True

    keywords = [w for w in words if w not in _STOP and not w.isdigit()]

    if re.search(r"\b(why|explain|how (?:does|did|is)|reason)\b", text) and tender_id:
        return Intent("explain", tender_id=tender_id)
    if tender_id and re.search(
        r"\b(details?|tell me|more about|deadline|buyer|link|read|summary|open)\b", text
    ):
        return Intent("details", tender_id=tender_id)
    if tender_id:
        return Intent("details", tender_id=tender_id)
    if re.search(r"\b(brief|briefing|morning|what'?s new|catch me up|best matche?s?|top picks?)\b", text):
        return Intent("brief", filters=filters, words=keywords)
    if re.search(r"\b(find|search|show|list|any|tenders?|opportunit\w+|bids?|procurement)\b", text):
        return Intent("search", filters=filters, words=keywords)
    return Intent("help")



# ----------------------------------------------------------------------------- result helpers
def find_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Find the list of tender summaries in a search result without assuming its key."""
    for key in ("tenders", "results", "items", "data"):
        v = result.get(key)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
    for v in result.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
            return [r for r in v if isinstance(r, dict)]
    return []


def _tender(result: Dict[str, Any]) -> Dict[str, Any]:
    t = result.get("tender")
    return t if isinstance(t, dict) else {}


def _score(analysis: Any) -> Optional[float]:
    if not isinstance(analysis, dict):
        return None
    for key in ("relevance_score", "score", "match_score"):
        v = analysis.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def _dig(obj: Any, key: str, depth: int = 3) -> Any:
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, "", []):
            return obj[key]
        if depth:
            for v in obj.values():
                found = _dig(v, key, depth - 1)
                if found is not None:
                    return found
    return None


def _clip(s: Any, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _row_phrase(row: Dict[str, Any]) -> str:
    bits = [_clip(row.get("title") or "Untitled", 90)]
    buyer = row.get("buyer")
    if buyer:
        bits.append(f"from {_clip(buyer, 60)}")
    score = _score(row.get("analysis"))
    if score is not None:
        bits.append(f"scored {_num(score)} out of 10")
    return ", ".join(bits)


def _is_mock(obj: Dict[str, Any]) -> bool:
    return bool(obj.get("is_mock"))


# ----------------------------------------------------------------------------- execution
class _Run:
    def __init__(self, client: Any, include_mock: bool):
        self.client = client
        self.include_mock = include_mock
        self.steps: List[Dict[str, Any]] = []
        self.sample = False
        self.fell_back = False
        self.discovery: Optional[Dict[str, Any]] = None

    async def discover(self) -> None:
        """Show MCP tools/list before tool calls and isolate discovery failures."""
        if not callable(getattr(type(self.client), "list_tools", None)):
            return
        step: Dict[str, Any] = {"tool": "tools/list", "arguments": {}}
        try:
            names, ms = await timed_list_tools(self.client)
            step.update(ok=True, ms=ms, summary=f"{len(names)} tool(s): " + ", ".join(names))
        except McpError as exc:
            logger.warning("MCP discovery failed (%s)", type(exc).__name__)
            step.update(ok=False, error="MCP discovery failed", summary="failed")
        except Exception as exc:
            # Discovery is optional; do not expose arbitrary client/server exception text.
            logger.warning("MCP discovery failed (%s)", type(exc).__name__)
            step.update(ok=False, error="MCP discovery failed", summary="failed")
        self.discovery = step

    async def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if len(self.steps) >= MAX_TOOL_CALLS:
            raise McpError("tool call budget for one request reached")
        step: Dict[str, Any] = {"tool": tool, "arguments": dict(args)}
        try:
            result, ms = await timed_call(self.client, tool, args)
        except McpError as exc:
            step.update(ok=False, error=str(exc), summary="failed")
            self.steps.append(step)
            raise
        ok = result.get("ok", True) is not False
        step.update(ok=ok, ms=ms)
        if not ok:
            err = result.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            step["summary"] = f"error: {_clip(msg or result.get('code') or 'unknown', 80)}"
        elif tool == "search_tenders":
            rows = find_rows(result)
            step["summary"] = f"{len(rows)} result(s)"
            self.sample = self.sample or any(_is_mock(r) for r in rows)
        else:
            t = _tender(result)
            step["summary"] = f"tender {t.get('id', args.get('tender_id'))}"
            self.sample = self.sample or _is_mock(t)
        self.steps.append(step)
        return result

    def search_args(self, intent: Intent, query: Optional[str], limit: int) -> Dict[str, Any]:
        args: Dict[str, Any] = {"include_mock": self.include_mock, "limit": limit}
        args.update(intent.filters)
        if query:
            args["query"] = query
        if intent.filters.get("fit_only") or "min_score" in intent.filters:
            args["analysis"] = "analyzed"
        return args


async def _search_with_fallback(run: _Run, intent: Intent, limit: int):
    """Try the whole phrase, then each keyword (longest first). Returns (rows, used_query, result)."""
    words = intent.words
    attempts: List[Optional[str]] = []
    if words:
        phrase = " ".join(words)
        attempts.append(phrase)
        if len(words) > 1:
            attempts.extend(sorted(set(words), key=lambda w: (-len(w), w)))
    else:
        attempts.append(None)
    last: Dict[str, Any] = {}
    for q in attempts[:4]:
        last = await run.call("search_tenders", run.search_args(intent, q, limit))
        if last.get("ok", True) is False:
            return [], q, last
        rows = find_rows(last)
        if rows:
            return rows, q, last
    used = attempts[min(len(attempts), 4) - 1]
    reserve = 2 if intent.name == "brief" else 0
    if words and len(run.steps) < MAX_TOOL_CALLS - reserve:
        last = await run.call("search_tenders", run.search_args(intent, None, limit))
        if last.get("ok", True) is not False:
            rows = find_rows(last)
            if rows:
                run.fell_back = True
                return rows, None, last
    return [], used, last


def _describe_filters(intent: Intent) -> str:
    f = intent.filters
    bits = []
    if f.get("source"):
        bits.append(f"from {f['source']}")
    if f.get("fit_only"):
        bits.append("marked as a good fit")
    if "min_score" in f:
        bits.append(f"scoring at least {_num(f['min_score'])}")
    return " ".join(bits)


def _explain_reply(result: Dict[str, Any], tender: Dict[str, Any]) -> str:
    title = _clip(tender.get("title") or "this tender", 90)
    score = _score(_dig(result, "analysis")) if isinstance(_dig(result, "analysis"), dict) else None
    if score is None:
        score = _score(_dig(result, "assessment")) if isinstance(_dig(result, "assessment"), dict) else None
    text = _dig(result, "explanation") or _dig(result, "reasoning") or _dig(result, "message")
    parts: List[str] = []
    if score is not None:
        parts.append(f"{title} was scored {_num(score)} out of 10.")
    else:
        parts.append(f"{title}.")
    if text and isinstance(text, str):
        parts.append(_clip(text, 220))
    for key, label in (
        ("matched_services", "Matching services"),
        ("eligibility_gaps", "Eligibility gaps"),
        ("suggested_next_steps", "Suggested next steps"),
    ):
        v = _dig(result, key)
        if isinstance(v, list) and v:
            parts.append(f"{label}: " + "; ".join(_clip(x, 60) for x in v[:3]) + ".")
    state = _dig(result, "state") or _dig(result, "analysis_state")
    if len(parts) == 1 and isinstance(state, str):
        parts.append(f"No stored AI assessment is available ({_clip(state, 40)}).")
    parts.append("This is the stored AI analysis, not verified.")
    return " ".join(parts)


def _details_reply(result: Dict[str, Any]) -> str:
    t = _tender(result)
    bits = [_clip(t.get("title") or "Untitled", 100) + "."]
    if t.get("buyer"):
        bits.append(f"Buyer: {_clip(t['buyer'], 70)}.")
    dl = t.get("deadline")
    if isinstance(dl, dict):
        dl = dl.get("date") or dl.get("text") or dl.get("raw")
    bits.append(f"Deadline: {_clip(dl, 60)}." if dl else "No deadline is published.")
    score = _score(t.get("analysis"))
    if score is not None:
        bits.append(f"Recorded relevance score {_num(score)} out of 10.")
    elif t.get("analysis_state"):
        bits.append(f"No AI score ({_clip(t['analysis_state'], 30)}).")
    return " ".join(bits)


HELP = (
    "Try: 'find tenders for health', 'show the newest World Bank tenders', "
    "'brief me on the best matches', or 'explain tender 3'."
)


async def run_intent(intent: Intent, client: Any, *, include_mock: bool = True) -> Turn:
    run = _Run(client, include_mock)
    if intent.name != "help":
        await run.discover()
    ids: List[int] = []
    reply = HELP
    try:
        if intent.name == "help":
            reply = HELP
        elif intent.name in ("search", "brief"):
            limit = 3 if intent.name == "brief" else 5
            rows, used, _ = await _search_with_fallback(run, intent, limit)
            ids = [r["id"] for r in rows if isinstance(r.get("id"), int)]
            topic = f" about {' '.join(intent.words)}" if intent.words else ""
            flt = _describe_filters(intent)
            if not rows:
                reply = f"I found no tenders{topic}{(' ' + flt) if flt else ''}. Try a broader keyword."
            else:
                lead = f"I found {len(rows)} tender{'s' if len(rows) != 1 else ''}{topic}"
                if run.fell_back:
                    lead = (f"I found nothing{topic}, so here {'are' if len(rows) != 1 else 'is'} "
                            f"the top {len(rows)} tender{'s' if len(rows) != 1 else ''} instead")
                elif used and intent.words and used != " ".join(intent.words):
                    lead += f" (broadened the search to '{used}')"
                if flt:
                    lead += f" {flt}"
                bits = [f"{lead}."]
                for n, row in enumerate(rows[:3], 1):
                    bits.append(f"Number {n}: {_row_phrase(row)}.")
                if intent.name == "brief" and ids:
                    det = await run.call("get_tender_details", {"tender_id": ids[0]})
                    exp = await run.call("explain_tender_match", {"tender_id": ids[0]})
                    if det.get("ok", True) is not False:
                        bits.append("About the top one: " + _details_reply(det))
                    if exp.get("ok", True) is not False:
                        bits.append(_explain_reply(exp, _tender(det) or rows[0]))
                else:
                    bits.append("Say 'explain the first one' or 'details of tender' with a number.")
                reply = " ".join(bits)
        elif intent.name == "details":
            det = await run.call("get_tender_details", {"tender_id": intent.tender_id})
            if det.get("ok", True) is False:
                reply = f"I couldn't get tender {intent.tender_id}: {run.steps[-1]['summary']}."
            else:
                ids = [intent.tender_id]
                reply = _details_reply(det)
        elif intent.name == "explain":
            exp = await run.call("explain_tender_match", {"tender_id": intent.tender_id})
            if exp.get("ok", True) is False:
                reply = f"I couldn't explain tender {intent.tender_id}: {run.steps[-1]['summary']}."
            else:
                ids = [intent.tender_id]
                reply = _explain_reply(exp, _tender(exp))
    except McpError as exc:
        reply = f"I couldn't reach the tender service: {exc}."
    if run.sample:
        reply = "Sample data. " + reply
    steps = ([run.discovery] if run.discovery else []) + run.steps
    return Turn(intent.name, reply, steps, {"ids": ids}, run.sample)
