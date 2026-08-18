# Jira MCP Test App (Flask)

Talks to your running `jira-mcp` Docker container (SSE endpoint, e.g.
`http://localhost:8000/sse`) and exposes simple REST + a browser UI for
read/write Jira operations.

## Prerequisites
- Your `jira-mcp` container from `docker compose up -d` must already be running.
- Python 3.10+

## Setup

```bash
cd jira_flask_app
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

## Configure

By default the app looks for the MCP server at `http://localhost:8000/sse`.
Override if needed:

```bash
set MCP_SERVER_URL=http://localhost:8000/sse   # Windows (cmd)
$env:MCP_SERVER_URL="http://localhost:8000/sse" # Windows (PowerShell)
```

## Run

```bash
python app.py
```

Open http://localhost:5000 in a browser.

## First step: confirm the real tool names

Different `mcp-atlassian` versions expose slightly different tool names and
parameter shapes. Before relying on the convenience endpoints, hit:

```
GET http://localhost:5000/api/tools
```

or click "Fetch tools" in the UI. This lists every tool the container
actually exposes, with its exact name and input schema. If any of
`jira_get_issue`, `jira_search`, `jira_create_issue`, `jira_add_comment`,
`jira_transition_issue` don't match what you see, either:

- edit the tool names/argument keys in `app.py`, or
- call `POST /api/call/<real_tool_name>` directly with a JSON body matching
  that tool's schema (the generic passthrough endpoint).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tools` | List all tools the MCP server exposes |
| GET | `/api/issues/<key>` | Read an issue |
| GET | `/api/search?jql=...` | Search issues via JQL |
| POST | `/api/issues` | Create issue (`project_key`, `summary`, `issue_type`, `description`) |
| POST | `/api/issues/<key>/comment` | Add a comment (`comment`) |
| POST | `/api/issues/<key>/transition` | Change status (`transition`) |
| POST | `/api/call/<tool_name>` | Generic passthrough to any tool |

## Notes

- This app opens a fresh SSE connection per request — simplest and most
  reliable for a POC. For production/high-traffic use, keep a persistent
  session in a background asyncio loop instead of reconnecting each call.
- `app.run(debug=True)` is for local dev only — don't ship that flag as-is.
