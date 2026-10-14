import sys
import os
import logging
import tempfile
from pathlib import Path
import requests
import certifi
from flask import has_request_context, session
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

def _configure_requests_ca_bundle():
    """Configure requests to trust the repo's corporate CA without
    monkey-patching ssl.

    The old Windows truststore injection could trigger deep recursion in
    Python 3.14/urllib3/ssl. Instead, we build a merged CA bundle from
    certifi + corporate-ca.crt (if present) and point REQUESTS_CA_BUNDLE at
    it. On Linux/Docker, keep using the system bundle created at build time.
    """
    if os.environ.get("REQUESTS_CA_BUNDLE"):
        return

    if sys.platform == "win32":
        base_bundle = Path(certifi.where())
        corporate_ca = Path(__file__).with_name("corporate-ca.crt")

        if corporate_ca.exists():
            merged_bundle = Path(tempfile.gettempdir()) / "cr-compliance-requests-ca-bundle.pem"
            try:
                merged_bundle.write_text(base_bundle.read_text(encoding="utf-8") + "\n" + corporate_ca.read_text(encoding="utf-8"), encoding="utf-8")
                os.environ["REQUESTS_CA_BUNDLE"] = str(merged_bundle)
                logger.info("Using merged CA bundle for requests: %s", merged_bundle)
                return
            except Exception as e:
                logger.warning("Could not build merged CA bundle (%s); falling back to certifi bundle", e)

        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
        logger.info("Using certifi CA bundle for requests: %s", certifi.where())
        return

    os.environ.setdefault("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")


_configure_requests_ca_bundle()


def _current_creds():
    if has_request_context():
        session_auth = session.get("jira_auth") or {}
        session_url = (session_auth.get("url") or "").strip()
        session_username = (session_auth.get("username") or "").strip()
        session_password = session_auth.get("password") or ""
        if session_url and session_username and session_password:
            return session_url, session_username, session_password

    return (
        os.environ.get("JIRA_URL"),
        os.environ.get("JIRA_USERNAME"),
        os.environ.get("JIRA_PASSWORD"),
    )

def get_issue_raw(issue_key):
    """Fetch the full raw issue JSON directly from Jira's REST API —
    bypassing mcp-atlassian's curated field subset — so custom fields
    (Complexity, RAG Status, etc.) are available for compliance checks.
    Raises on missing credentials or a failed request."""
    jira_url, jira_username, jira_password = _require_creds()
    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{issue_key}"
    resp = requests.get(url, auth=HTTPBasicAuth(jira_username, jira_password), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _require_creds():
    jira_url, jira_username, jira_password = _current_creds()
    if not all([jira_url, jira_username, jira_password]):
        raise RuntimeError(
            "Jira credentials are not available for direct field access. "
            "Please log in again from the login page so the current Flask session "
            "has JIRA_URL, JIRA_USERNAME, and JIRA_PASSWORD."
        )
    return jira_url, jira_username, jira_password


def _auth():
    _, jira_username, jira_password = _require_creds()
    return HTTPBasicAuth(jira_username, jira_password)


def get_available_transitions(issue_key):
    """Returns the list of statuses this issue can ACTUALLY move to right
    now, per Jira's own workflow — never a hardcoded guess. Each entry:
    {"id": "...", "name": "...", "to_status": "..."}"""
    jira_url, _, _ = _require_creds()
    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{issue_key}/transitions"
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
    jira_url, _, _ = _require_creds()
    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{issue_key}/transitions"
    resp = requests.post(url, json={"transition": {"id": transition_id}}, auth=_auth(), timeout=30)
    resp.raise_for_status()
    return {"success": True}


def update_issue_fields(issue_key, fields_payload):
    """fields_payload: dict of {field_id: value}, already shaped correctly
    for Jira's REST API (see field_rules.format_field_value) — e.g. a
    select-type field needs {"value": "High"}, plain text/dates are the
    raw value, labels are a list."""
    jira_url, _, _ = _require_creds()
    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{issue_key}"
    resp = requests.put(url, json={"fields": fields_payload}, auth=_auth(), timeout=30)
    if not resp.ok:
        # Jira's error body has the actual reason (which field, why) —
        # without this, all we'd see is a generic "400 Bad Request",
        # which isn't enough to know what to fix.
        detail = None
        try:
            body = resp.json()
            parts = list(body.get("errorMessages", []))
            for field_id, msg in (body.get("errors") or {}).items():
                parts.append(f"{field_id}: {msg}")
            if parts:
                detail = "; ".join(parts)
        except Exception:
            pass
        raise RuntimeError(f"Jira rejected the update ({resp.status_code}): {detail or resp.text[:300]}")
    return {"success": True}
def get_projects():
    """Return only Jira projects whose key starts with ACC."""
    jira_url, _, _ = _require_creds()

    url = f"{jira_url.rstrip('/')}/rest/api/2/project"
    projects = []
    start_at = 0

    while True:
        try:
            resp = requests.get(
                url,
                params={"startAt": start_at, "maxResults": 50},
                auth=_auth(),
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()

            # Jira Data Center: response is a direct array
            if isinstance(data, list):
                logger.info(
                    "get_projects: Received Jira Data Center format with %d projects",
                    len(data)
                )

                for p in data:
                    key = p.get("key")

                    if key and key.upper() == "ACC":
                        projects.append({
                            "key": key,
                            "name": p.get("name")
                        })

                break

            # Jira Cloud: paginated response
            else:
                values = data.get("values", [])

                logger.info(
                    "get_projects: Received Jira Cloud format with %d projects in this page",
                    len(values)
                )

                for p in values:
                    key = p.get("key")

                if key and key.upper() == "ACC":
                        projects.append({
                            "key": key,
                            "name": p.get("name")
                        })

                if data.get("isLast", True):
                    break

                start_at += data.get("maxResults", 50)

        except Exception as e:
            logger.error(
                "Error fetching projects: %s",
                e,
                exc_info=True
            )
            raise

    logger.info(
        "get_projects: Returning %d ACC projects",
        len(projects)
    )

    return projects

def get_projects11():
    """Every Jira project these credentials can see. Jira Data Center returns
    a JSON array directly, while Cloud returns a paginated response with values."""
    jira_url, _, _ = _require_creds()
    url = f"{jira_url.rstrip('/')}/rest/api/2/project"
    projects, start_at = [], 0
    while True:
        try:
            resp = requests.get(url, params={"startAt": start_at, "maxResults": 50}, auth=_auth(), timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Handle both Jira Data Center (direct array) and Cloud (paginated with values key)
            if isinstance(data, list):
                # Jira Data Center: response is a direct array
                logger.info("get_projects: Received Jira Data Center format (direct array) with %d projects", len(data))
                for p in data:
                    projects.append({"key": p.get("key"), "name": p.get("name")})
                break  # No pagination in direct array response
            else:
                # Jira Cloud: response is a dict with values key
                values = data.get("values", [])
                logger.info("get_projects: Received Jira Cloud format (paginated) with %d projects in this page", len(values))
                for p in values:
                    projects.append({"key": p.get("key"), "name": p.get("name")})
                if data.get("isLast", True):
                    break
                start_at += data.get("maxResults", 50)
        except Exception as e:
            logger.error("Error fetching projects: %s", e, exc_info=True)
            raise

    logger.info("get_projects: Returning %d total projects", len(projects))
    return projects


def get_boards(project_key=None):
    """Boards (Scrum/Kanban) visible to these credentials, optionally
    scoped to one project. Boards live under Jira's separate Agile REST
    API (/rest/agile/1.0/), not the standard /rest/api/2/ used
    everywhere else in this file — a board isn't a JQL-queryable concept,
    it's tied to a filter configured on the board itself.
    Handles both Jira Data Center (direct array) and Cloud (paginated) responses."""
    jira_url, _, _ = _require_creds()
    url = f"{jira_url.rstrip('/')}/rest/agile/1.0/board"
    params = {"maxResults": 50}
    if project_key:
        params["projectKeyOrId"] = project_key
    boards, start_at = [], 0
    while True:
        try:
            params["startAt"] = start_at
            resp = requests.get(url, params=params, auth=_auth(), timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Handle both Jira Data Center (direct array) and Cloud (paginated with values key)
            if isinstance(data, list):
                # Jira Data Center: response is a direct array
                logger.info("get_boards: Received Jira Data Center format (direct array) with %d boards for project %s", len(data), project_key)
                for b in data:
                    boards.append({"id": b.get("id"), "name": b.get("name"), "type": b.get("type")})
                break  # No pagination in direct array response
            else:
                # Jira Cloud: response is a dict with values key
                values = data.get("values", [])
                logger.info("get_boards: Received Jira Cloud format (paginated) with %d boards in this page for project %s", len(values), project_key)
                for b in values:
                    boards.append({"id": b.get("id"), "name": b.get("name"), "type": b.get("type")})
                if data.get("isLast", True):
                    break
                start_at += data.get("maxResults", 50)
        except Exception as e:
            logger.error("Error fetching boards for project %s: %s", project_key, e, exc_info=True)
            raise

    logger.info("get_boards: Returning %d total boards for project %s", len(boards), project_key)
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
        "summary": fields.get("summary"),
    }


def search_board_issues(board_id, jql=None, max_pages=50):
    """All issues on a board, optionally further filtered by jql (e.g.
    restricting to CR/Epic/Outcome types) — the Agile API's board/issue
    endpoint accepts an additional jql param layered on top of the
    board's own configured filter. Paginates via startAt/isLast (a
    different pagination style than mcp-atlassian's page_token, since
    this is a different API family). Returns issues already normalized
    to match the rest of the scan code's expected shape."""
    jira_url, _, _ = _require_creds()
    url = f"{jira_url.rstrip('/')}/rest/agile/1.0/board/{board_id}/issue"
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
