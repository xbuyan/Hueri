import asyncio

from demo_backend import router

ROW = {"id": 2, "title": "Environmental audit", "buyer": "Sample Board", "is_mock": True,
       "analysis": {"relevance_score": 8.4}}


def _fake(empty_for):
    calls = []

    async def fake(client, tool, args):
        calls.append((tool, dict(args)))
        if tool == "search_tenders" and args.get("query") in empty_for:
            return {"ok": True, "tenders": []}, 5
        if tool == "search_tenders":
            return {"ok": True, "tenders": [ROW]}, 5
        return {"ok": True, "tender": ROW}, 5

    return fake, calls


def test_open_is_filler():
    assert router.parse("What tenders are open in Kenya?").words == ["kenya"]


def test_falls_back_to_unfiltered_listing(monkeypatch):
    fake, calls = _fake(empty_for={"kenya"})
    monkeypatch.setattr(router, "timed_call", fake)
    turn = asyncio.run(router.run_intent(router.parse("What tenders are open in Kenya?"), None))
    assert turn.reply.startswith("Sample data. I found nothing about kenya")
    assert turn.context["ids"] == [2]
    assert "query" not in calls[-1][1] and calls[-1][1]["include_mock"] is True
    assert len(calls) == 2


def test_nothing_anywhere_keeps_old_message(monkeypatch):
    fake, calls = _fake(empty_for={"kenya", None})
    monkeypatch.setattr(router, "timed_call", fake)
    turn = asyncio.run(router.run_intent(router.parse("find kenya tenders"), None))
    assert "found no tenders" in turn.reply and turn.context["ids"] == []


def test_best_matches_is_a_brief():
    for phrase in ("What are the best matches?", "show me the best match", "any top picks"):
        assert router.parse(phrase).name == "brief", phrase
