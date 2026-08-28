# Jira Compliance Dashboard (Flask + Jira MCP)

This project connects to Jira through an MCP server and direct Jira REST APIs,
then builds a compliance dashboard for CR/Story lifecycle alignment.

The dashboard supports scoped views by:

- Jira project (default: `ACC`)
- Jira board
- assignee

It highlights non-compliant CR/Story pairs and also shows CR/Outcome field
quality findings.

## Prerequisites

- Python `3.10+`
- A running Jira MCP endpoint (for example `http://localhost:8000/sse`)
- Jira credentials with permission to read project/board/issues

## Setup (Windows PowerShell)

```powershell
Set-Location "D:\Homeoffice\Jira-Dashboard\CR-compliance-dashboard"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration

Set environment variables (or use `jira.env` in this repo root):

```powershell
$env:MCP_SERVER_URL="http://localhost:8000/sse"
$env:JIRA_URL="https://your-domain.atlassian.net"
$env:JIRA_USERNAME="your.user@company.com"
$env:JIRA_PASSWORD="your-jira-password-or-token"

# Optional defaults
$env:DEFAULT_PROJECT_KEY="ACC"
$env:CR_DISCOVERY_JQL='labels = "CR"'
```

Notes:

- Direct REST helpers in `jira_rest.py` use basic auth (`JIRA_USERNAME` + `JIRA_PASSWORD` or `JIRA_API_TOKEN`).
- `CR_DISCOVERY_JQL` is attempted first; fallback discovery tries CR issue types.

## Run

```powershell
Set-Location "D:\Homeoffice\Jira-Dashboard\CR-compliance-dashboard"
python app.py
```

Open `http://localhost:5000/dashboard`.

## Dashboard Workflow

1. Select Project (`ACC` by default).
2. Select Jira Board.
3. (Optional) Select Assignee.
4. Click `Refresh now` to run a scoped scan.
5. Review:
   - **CR tab**: non-compliant CRs (filtered scope)
   - **Story tab**: non-compliant Story/CR pairs

If only board is selected, scope is board-only. If assignee is selected,
results are further filtered to that assignee's CR or story assignments.

## API Endpoints

### Core dashboard

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/dashboard/refresh` | Run scoped scan (`project_key`, `board_id`) |
| GET | `/api/dashboard/summary` | Get non-compliant Story/CR summary (supports `project_key`, `board_id`, `assignee`, `debug`) |
| GET | `/api/crs` | Get CR list (supports `project_key`, `board_id`, `assignee`, `non_compliant_only`) |

### Scope selectors

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/jira/projects` | Load available projects |
| GET | `/api/jira/boards?project_key=ACC` | Load boards for project |
| GET | `/api/jira/assignees?project_key=ACC&board_id=2078` | Load assignees from scoped CRs and linked stories |

### MCP/Jira utility endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tools` | List MCP tools |
| GET | `/api/search?jql=...` | Search issues via MCP |
| GET | `/api/issues/<key>` | Read issue |
| POST | `/api/issues` | Create issue |
| POST | `/api/issues/<key>/comment` | Add comment |
| POST | `/api/issues/<key>/transition` | Transition issue |
| POST | `/api/call/<tool_name>` | Generic MCP passthrough |

## How the Code Works (Quick Explanation)

The Flask app wraps Jira data retrieval and compliance evaluation behind simple
JSON endpoints. The route structure makes this explicit:

```python
@app.route("/api/dashboard/summary", methods=["GET"])
def dashboard_summary():
    ...
```

That summary endpoint reads the latest scan results from SQLite and applies
scope filters (`project_key`, `board_id`, `assignee`) before returning active
non-compliant rows.

CR discovery is scope-aware and tries multiple JQL candidates, so boards that
use different CR conventions still resolve:

```python
candidate_jqls, custom_jql_configured = _cr_discovery_jql_candidates(project_key)
```

Once CRs are found, the scanner follows linked issues and evaluates CR->Story
alignment using phase rules. The result payload stores both statuses and
assignees for filtering and display:

```python
results.append({
    "pair_type": "cr_story",
    "cr_key": cr_key,
    "story_key": story_key,
    "cr_assignee": cr_assignee,
    "story_assignee": _assignee_name(issue),
    ...
})
```

The non-compliance rule itself is phase-window based in `compliance.py`.
Conceptually, Story status must stay in the allowed phase window for the CR
status. If it is ahead/behind (or blocked/withdrawn inconsistently), the pair
is marked non-compliant with severity and score.

## Debugging Scoped Results

Use debug mode to inspect the effective scope and non-compliant CR keys:

```powershell
curl "http://localhost:5000/api/dashboard/summary?project_key=ACC&board_id=2078&assignee=Jane%20Doe&debug=1"
```

Use these to compare scan scope and CR list:

```powershell
curl "http://localhost:5000/api/crs?project_key=ACC&board_id=2078&assignee=Jane%20Doe&non_compliant_only=1"
curl "http://localhost:5000/api/dashboard/summary?project_key=ACC&board_id=2078&assignee=Jane%20Doe"
```

## Notes

- The app uses short-lived SSE sessions per request for simplicity.
- `app.run(debug=True)` is for local development only.
