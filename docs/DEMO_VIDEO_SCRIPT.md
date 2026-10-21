# Demo video script (target 2:40, hard limit 3:00)

Record the screen at 1080p. Use Chrome or Edge if you want voice input; otherwise type.
Before recording: start both servers, click **Start over**, set browser zoom so the chat and the
trace panel are both readable, close other tabs and hide bookmarks.

| Time | On screen | Say |
|---|---|---|
| 0:00-0:15 | Title card or the page | "HUERI is an environmental consultancy in Kenya. Its tender scout finds procurement notices and scores them. Today it can answer a voice assistant through MCP." |
| 0:15-0:40 | Terminal: curl `initialize`, `tools/list` (see docs/MCP_DEMO.md) | "This is a real Streamable HTTP MCP server. A client initializes, lists three read-only tools, and calls them." |
| 0:40-1:05 | Page: type "Find environmental audit tenders" | "I ask in plain language. The assistant calls search_tenders. Every call shows on the right, with arguments and timing. The badge says this is sample data." |
| 1:05-1:30 | "Explain the first one" | "Now it uses the result it just found and calls explain_tender_match. The explanation is the stored AI analysis, and it says so." |
| 1:30-2:05 | "What are the best matches?" | "One question, three tools: search, details, explanation, chained automatically. That is orchestration, not a wrapper around one endpoint." |
| 2:05-2:25 | "What tenders are open in Kenya?" | "If nothing matches, it says so and offers the top sample tenders instead of failing." |
| 2:25-2:45 | Terminal: `check` summary line, then `verify` | "The server rejects missing tokens and bad hosts, and the database role is read-only. It cannot write or read user accounts." |
| 2:45-3:00 | Title card | "A read-only MCP service for tender intelligence. Sample data in this demo; the same server can point at live data." |

## Recording checklist

- Say "sample data" out loud at least once.
- Do not show `demo.env`, `.env`, or any token. In the curl demo, `$TOKEN` appears as a variable, never its value.
  Do not run `curl -v`.
- Keep the video under 3:00 and make it public (check the contest rules for the hosting site).

## Devpost text to prepare

- **What it does:** copy the "What it does" and "Architecture" sections of docs/MCP_DEMO.md.
- **Setup instructions:** link to the "Run it locally" section.
- **Product feedback:** write from your own experience building this (what was easy, what was confusing in
  the MCP SDK, transport and auth). I can draft it from the notes in this chat if you tell me what stood out.
- **Significant update explanation:** the MCP service, the simulated Alexa client and the tests were all
  added during the contest period, on top of the existing tender scout (check the contest rules for exactly
  what must be described). Commits on the `mcp-service` branch show this.
