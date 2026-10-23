# HUERI Tender Scout: read-only MCP server and simulated Alexa+ demo

This branch adds a **read-only Model Context Protocol server** (Streamable HTTP) on top of the
HUERI tender scout, plus a **simulated Alexa+** web page that talks to it through real MCP calls.

> All tenders in the demo are **sample data** (flagged `is_mock`). The demo never shows live tenders.
> The Alexa part is a simulation (typed or browser-voice input, spoken reply). It is not an
> Alexa skill and does no account linking.

## What it does

An assistant can ask the tender database three things, each a real MCP tool:

| Tool | Purpose |
|---|---|
| `search_tenders` | Find tenders by keyword, source, minimum score, fit-only, sort. Paginated. |
| `get_tender_details` | One tender: buyer, deadline (free text as published), stored analysis. |
| `explain_tender_match` | The stored AI assessment of why a tender does or does not fit HUERI. |

Hueri exposes a self-hosted MCP server over Streamable HTTP. The simulated Alexa+ client performs
real MCP protocol operations (`initialize`, `tools/list`, `tools/call`) and runs a multi-step MCP
workflow: for "Brief me on the best matches" it searches, then passes the tender ID returned by the
search into the details and explanation tools when a result is available. The trace shows the
`tools/list` discovery step and tool calls, with their arguments and timing. It does not display the
`initialize` request or `notifications/initialized` protocol step that precede each operation.

## Architecture

```
Browser (simulated Alexa page, port 8800)
   -> demo_backend (FastAPI): deterministic router, holds the bearer token
        -> MCP server (port 8765, Streamable HTTP, stateless JSON, mcp==1.30.0)
             -> Postgres (read-only role: SELECT on tenders and tender_analyses only)
```

- The browser never sees the MCP token; the demo backend adds it server-side.
- The MCP package imports only `app.models`. It has no access to the scoring agent, notifier,
  collectors, worker, or any write path.
- No LLM is involved in routing or tool selection. A deterministic router maps each request to tool
  calls. Follow-up calls can depend on earlier results: a "brief" request calls details and
  explanation for the first returned tender when search provides an ID. Thus the same input can
  result in different tool calls when search results differ. (An LLM router could produce the same
  intents later; it is not part of this submission.)

## Security behaviour (verified by `check` and `verify`)

- No token, wrong token, or a token in the query string: `401`.
- Unknown `Host`: `421`. Unknown `Origin`: `403` (allowlists come from the environment).
- `GET` on the endpoint: `405`. No session ids are issued (stateless).
- The database role cannot write, cannot read `users`, and cannot create tables.
- Secrets live outside the repository in `~/.config/hueri-mcp/` (mode 0600).
- Fields the system does not store (for example confidence or disqualification risk) are never claimed.

## Run it locally

Requirements: Docker, Python 3.12 or newer. Tested locally on Python 3.14.4; CI runs on Python 3.12.
Ports used: 55432 (throwaway Postgres), 8765 (MCP), 8800 (demo page). It touches no other container.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/python scripts/mcp_demo.py up       # throwaway Postgres + SELECT-only role
.venv/bin/python scripts/mcp_demo.py seed     # sample tenders (flagged is_mock)
.venv/bin/python scripts/mcp_demo.py verify   # proves the role cannot write or read users

# terminal A (leave running)
.venv/bin/python scripts/mcp_demo.py run

# terminal B (leave running)
.venv/bin/python -m uvicorn demo_backend.app:app --host 127.0.0.1 --port 8800

# terminal C
.venv/bin/python scripts/mcp_demo.py check    # protocol, auth and worker checks
```

Open http://127.0.0.1:8800/ and try: "Find environmental audit tenders", "Explain the first one",
"Details of tender 1", "What are the best matches?".

Voice input uses the browser's speech recognition (Chrome or Edge). Brave blocks it; typing always works.
Remove everything with `scripts/mcp_demo.py down`.

## Call the MCP server directly (without the page)

```bash
TOKEN=$(grep '^MCP_BEARER_TOKEN=' ~/.config/hueri-mcp/demo.env | cut -d= -f2)
H=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'
   -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2025-11-25')
U=http://127.0.0.1:8765/mcp

curl -s "${H[@]}" $U -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
curl -s "${H[@]}" $U -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
curl -s "${H[@]}" $U -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
curl -s "${H[@]}" $U -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_tenders","arguments":{"query":"environmental audit","include_mock":true,"limit":3}}}'
```

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

## Honest limits

- Simulated Alexa+, not a published skill; no OAuth or account linking.
- Sample data only in the demo. Connecting it to HUERI's live database is a configuration change
  (read-only role and `MCP_DATABASE_URL`), not a code change.
- The main product's `GET /api/tenders` and `GET /api/analytics/summary` are not yet behind login;
  that is planned work and is not part of the MCP service.
