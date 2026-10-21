"""FastAPI demo backend. Run:  .venv/bin/python -m uvicorn demo_backend.app:app --port 8800

The browser only ever talks to this app. The bearer token stays here, server-side.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import DemoSettings
from .mcp_client import McpClient
from .router import parse, run_intent

app = FastAPI(title="Hueri demo backend", docs_url=None, redoc_url=None, openapi_url=None)
_settings = DemoSettings.load()


class AskBody(BaseModel):
    utterance: str = Field(max_length=500)
    context: Optional[Dict[str, Any]] = None


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "mcp_url": _settings.mcp_url,
        "token_configured": _settings.token is not None,
        "token_source": _settings.token_source,
        "include_mock": _settings.include_mock,
    }


@app.post("/api/ask")
async def ask(body: AskBody) -> Dict[str, Any]:
    if not _settings.token:
        return {
            "intent": "error", "sample_data": False, "steps": [], "context": {"ids": []},
            "reply": "The demo backend has no MCP token. Run the demo script's 'up', or set DEMO_MCP_TOKEN.",
        }
    clean_ctx = {"ids": [i for i in (body.context or {}).get("ids", []) if isinstance(i, int)][:25]}
    intent = parse(body.utterance, clean_ctx)
    turn = await run_intent(intent, McpClient(_settings.mcp_url, _settings.token),
                            include_mock=_settings.include_mock)
    return {
        "intent": turn.intent, "reply": turn.reply, "steps": turn.steps,
        "context": turn.context, "sample_data": turn.sample_data,
    }

from demo_backend.web_mount import mount_web  # noqa: E402
mount_web(app)
