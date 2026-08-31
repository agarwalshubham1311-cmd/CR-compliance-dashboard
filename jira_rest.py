import sys
import os
import requests
from requests.auth import HTTPBasicAuth

# SSL trust setup differs by platform:
#
# - Windows (native venv): no easy way to point `requests` at the OS trust
#   store via an env var, so `truststore` globally patches ssl.SSLContext to
#   read it directly. Safe here because jira_rest.py is the main thing using
#   `requests` heavily in that setup.
#
# - Linux (Docker): the corporate CA is already merged into the system bundle
#   by `update-ca-certificates` at build time (see Dockerfile). We do NOT use
#   truststore's global monkey-patch here — inside the container, multiple
#   libraries touch the SSL layer in the same process (requests, httpx/MCP,
#   Ollama's client), and truststore's patch has been observed to cause
#   "maximum recursion depth exceeded" in that combination. Pointing
#   REQUESTS_CA_BUNDLE at the system bundle achieves the same trust result
#   without any monkey-patching.
if sys.platform == "win32":
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass  # falls back to REQUESTS_CA_BUNDLE / certifi if truststore isn't installed
else:
    os.environ.setdefault("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")

JIRA_URL = os.environ.get("JIRA_URL")
JIRA_USERNAME = os.environ.get("JIRA_USERNAME")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")


def get_issue_raw(issue_key):
    """Fetch the full raw issue JSON directly from Jira's REST API —
    bypassing mcp-atlassian's curated field subset — so custom fields
    (Complexity, RAG Status, etc.) are available for compliance checks.
    Raises on missing credentials or a failed request."""
    _require_creds()
    url = f"{JIRA_URL.rstrip('/')}/rest/api/3/issue/{issue_key}"
    resp = requests.get(url, auth=HTTPBasicAuth(JIRA_USERNAME, JIRA_API_TOKEN), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _require_creds():
    if not all([JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN]):
        raise RuntimeError(
            "JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN must be set in the Flask "
            "app's own environment (not just jira.env for the Docker container) "
            "for direct field access to work."
        )


def _auth():
    return HTTPBasicAuth(JIRA_USERNAME, JIRA_API_TOKEN)


def get_available_transitions(issue_key):
    """Returns the list of statuses this issue can ACTUALLY move to right
    now, per Jira's own workflow — never a hardcoded guess. Each entry:
    {"id": "...", "name": "...", "to_status": "..."}"""
    _require_creds()
    url = f"{JIRA_URL.rstrip('/')}/rest/api/3/issue/{issue_key}/transitions"
    resp = requests.get(url, auth=_auth(), timeout=30)
    resp.raise_for_status()
    transitions = resp.json().get("transitions", [])
    return [
        {"id": t["id"], "name": t.get("name", ""), "to_status": t.get("to", {}).get("name", "")}
        for t in transitions
    ]


def transition_issue(issue_key, transition_id):
    """Applies a transition by ID (from get_available_transitions) — never
    by status name directly, since Jira requires the specific transition
    ID, and validating against the real available list prevents silently
    failed or wrong-workflow writes."""
    _require_creds()
    url = f"{JIRA_URL.rstrip('/')}/rest/api/3/issue/{issue_key}/transitions"
    resp = requests.post(url, json={"transition": {"id": transition_id}}, auth=_auth(), timeout=30)
    resp.raise_for_status()
    return {"success": True}


def update_issue_fields(issue_key, fields_payload):
    """fields_payload: dict of {field_id: value}, already shaped correctly
    for Jira's REST API (see field_rules.format_field_value) — e.g. a
    select-type field needs {"value": "High"}, plain text/dates are the
    raw value, labels are a list."""
    _require_creds()
    url = f"{JIRA_URL.rstrip('/')}/rest/api/3/issue/{issue_key}"
    resp = requests.put(url, json={"fields": fields_payload}, auth=_auth(), timeout=30)
    resp.raise_for_status()
    return {"success": True}


def get_projects():
    """Every Jira project these credentials can see. Uses the paginated
    project/search endpoint (the older /project endpoint is deprecated)."""
    _require_creds()
    url = f"{JIRA_URL.rstrip('/')}/rest/api/3/project/search"
    projects, start_at = [], 0
    while True:
        resp = requests.get(url, params={"startAt": start_at, "maxResults": 50}, auth=_auth(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("values", []):
            projects.append({"key": p.get("key"), "name": p.get("name")})
        if data.get("isLast", True):
            break
        start_at += data.get("maxResults", 50)
    return projects


def get_boards(project_key=None):
    """Boards (Scrum/Kanban) visible to these credentials, optionally
    scoped to one project. Boards live under Jira's separate Agile REST
    API (/rest/agile/1.0/), not the standard /rest/api/3/ used
    everywhere else in this file — a board isn't a JQL-queryable concept,
    it's tied to a filter configured on the board itself."""
    _require_creds()
    url = f"{JIRA_URL.rstrip('/')}/rest/agile/1.0/board"
    params = {"maxResults": 50}
    if project_key:
        params["projectKeyOrId"] = project_key
    boards, start_at = [], 0
    while True:
        params["startAt"] = start_at
        resp = requests.get(url, params=params, auth=_auth(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for b in data.get("values", []):
            boards.append({"id": b.get("id"), "name": b.get("name"), "type": b.get("type")})
        if data.get("isLast", True):
            break
        start_at += data.get("maxResults", 50)
    return boards


def _normalize_agile_issue(raw):
    """The Agile API returns issues in the standard Jira REST shape
    ({"key": ..., "fields": {"status": {...}, "issuetype": {...}}}) —
    different from mcp-atlassian's flattened tool-response shape
    ({"key": ..., "status": {...}, "issue_type": {...}}) that the rest
    of the scan code (app.py's _run_full_scan) already expects. This
    normalizes one Agile API issue into that same flattened shape, so
    board-scoped and non-board-scoped scans can share all the same
    downstream processing without any branching."""
    fields = raw.get("fields", {}) or {}
    return {
        "key": raw.get("key"),
        "status": fields.get("status") or {},
        "issue_type": fields.get("issuetype") or {},
    }


def search_board_issues(board_id, jql=None, max_pages=50):
    """All issues on a board, optionally further filtered by jql (e.g.
    restricting to CR/Epic/Outcome types) — the Agile API's board/issue
    endpoint accepts an additional jql param layered on top of the
    board's own configured filter. Paginates via startAt/isLast (a
    different pagination style than mcp-atlassian's page_token, since
    this is a different API family). Returns issues already normalized
    to match the rest of the scan code's expected shape."""
    _require_creds()
    url = f"{JIRA_URL.rstrip('/')}/rest/agile/1.0/board/{board_id}/issue"
    params = {"maxResults": 50}
    if jql:
        params["jql"] = jql
    issues, start_at = [], 0
    for _ in range(max_pages):
        params["startAt"] = start_at
        resp = requests.get(url, params=params, auth=_auth(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        issues.extend(_normalize_agile_issue(i) for i in data.get("issues", []))
        if data.get("isLast", True) or not data.get("issues"):
            break
        start_at += data.get("maxResults", 50)
    return issues
