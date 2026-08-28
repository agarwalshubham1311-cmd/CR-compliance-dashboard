import os
import requests
from requests.auth import HTTPBasicAuth

# Use the Windows (or macOS/Linux) OS certificate store for SSL verification
# instead of Python's bundled certifi list. This picks up corporate root AND
# intermediate certificates automatically — the same trust chain your browser
# already uses — avoiding "missing intermediate certificate" errors that a
# manually-exported single root cert can hit.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # falls back to REQUESTS_CA_BUNDLE / certifi if truststore isn't installed

def _get_jira_auth_config():
    jira_url = os.environ.get("JIRA_URL")
    jira_username = os.environ.get("JIRA_USERNAME")
    jira_password = os.environ.get("JIRA_PASSWORD")
    jira_api_token = os.environ.get("JIRA_API_TOKEN")

    # Prefer password for Jira Data Center / Server; fall back to API token.
    auth_secret = jira_password or jira_api_token
    return jira_url, jira_username, auth_secret


def _auth_tuple():
    jira_url, jira_username, auth_secret = _get_jira_auth_config()
    if not all([jira_url, jira_username, auth_secret]):
        raise RuntimeError(
            "JIRA_URL, JIRA_USERNAME, and one of JIRA_PASSWORD/JIRA_API_TOKEN "
            "must be set for direct Jira access."
        )
    return jira_url, HTTPBasicAuth(jira_username, auth_secret)


def _get_json(url, auth, params=None, timeout=30):
    resp = requests.get(url, auth=auth, params=params or {}, timeout=timeout)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        if resp.status_code in (401, 403):
            raise RuntimeError(
                "Jira authentication failed. Verify JIRA_USERNAME and JIRA_PASSWORD/JIRA_API_TOKEN, then restart the app."
            ) from exc
        raise
    return resp.json() if resp.content else {}


def _normalize_issue(issue):
    fields = issue.get("fields") or {}
    assignee = fields.get("assignee") or issue.get("assignee") or {}
    labels = fields.get("labels") or issue.get("labels") or []
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary") or issue.get("summary") or "",
        "updated": fields.get("updated") or issue.get("updated"),
        "status": {
            "name": ((fields.get("status") or {}).get("name")) or ((issue.get("status") or {}).get("name")),
        },
        "issue_type": {
            "name": ((fields.get("issuetype") or {}).get("name")) or ((issue.get("issue_type") or {}).get("name")),
        },
        "assignee": {
            "displayName": assignee.get("displayName") or assignee.get("name") or "",
            "accountId": assignee.get("accountId") or "",
        },
        "labels": labels if isinstance(labels, list) else [],
    }


def search_issues(jql, max_results=200, fields=None, timeout=30):
    """Search Jira directly over REST and normalize issues to the shape used by app.py."""
    jira_url, auth = _auth_tuple()
    url = f"{jira_url.rstrip('/')}/rest/api/2/search"
    requested_fields = fields or ["summary", "status", "issuetype", "updated", "assignee"]
    resp = requests.get(
        url,
        auth=auth,
        params={
            "jql": jql,
            "maxResults": max_results,
            "fields": ",".join(requested_fields),
        },
        timeout=timeout,
    )
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        if resp.status_code in (401, 403):
            raise RuntimeError(
                "Jira authentication failed while searching issues. "
                "Verify JIRA_USERNAME and JIRA_PASSWORD/JIRA_API_TOKEN, then restart the app."
            ) from exc
        raise

    payload = resp.json() if resp.content else {}
    issues = payload.get("issues") or []
    return [_normalize_issue(issue) for issue in issues]


def list_projects(timeout=30):
    """List Jira projects visible to the configured user."""
    jira_url, auth = _auth_tuple()

    projects = []
    start_at = 0
    max_results = 100
    while True:
        payload = _get_json(
            f"{jira_url.rstrip('/')}/rest/api/2/project/search",
            auth,
            params={"startAt": start_at, "maxResults": max_results},
            timeout=timeout,
        )
        values = payload.get("values") or []
        projects.extend(values)
        if payload.get("isLast", True) or not values:
            break
        start_at += max_results

    return [
        {
            "id": str(project.get("id") or ""),
            "key": project.get("key") or "",
            "name": project.get("name") or "",
        }
        for project in projects
        if project.get("key")
    ]


def list_boards(project_key=None, timeout=30):
    """List Jira boards, optionally filtered by project key."""
    jira_url, auth = _auth_tuple()
    boards = []
    start_at = 0
    max_results = 50

    while True:
        params = {"startAt": start_at, "maxResults": max_results}
        if project_key:
            params["projectKeyOrId"] = project_key
        payload = _get_json(
            f"{jira_url.rstrip('/')}/rest/agile/1.0/board",
            auth,
            params=params,
            timeout=timeout,
        )
        values = payload.get("values") or []
        boards.extend(values)
        if payload.get("isLast", True) or not values:
            break
        start_at += max_results

    return [
        {
            "id": board.get("id"),
            "name": board.get("name") or "",
            "type": board.get("type") or "",
        }
        for board in boards
        if board.get("id") is not None
    ]


def search_board_issues(board_id, jql=None, max_results=200, fields=None, timeout=30):
    """Search issues within a Jira board scope via Agile API."""
    jira_url, auth = _auth_tuple()
    requested_fields = fields or ["summary", "status", "issuetype", "updated", "assignee"]
    issues = []
    start_at = 0
    target_limit = max(int(max_results or 200), 1)
    # Jira Agile board issue API is stricter than /search; large maxResults
    # values (e.g. 500) can produce HTTP 400 in some Jira instances.
    page_size = min(target_limit, 100)

    while len(issues) < target_limit:
        remaining = target_limit - len(issues)
        payload = _get_json(
            f"{jira_url.rstrip('/')}/rest/agile/1.0/board/{board_id}/issue",
            auth,
            params={
                "jql": jql or "",
                "startAt": start_at,
                "maxResults": min(page_size, remaining),
                "fields": ",".join(requested_fields),
            },
            timeout=timeout,
        )
        page = payload.get("issues") or []
        if not page:
            break
        issues.extend(page)

        # Agile API commonly exposes isLast; fall back to total/startAt math.
        if payload.get("isLast") is True:
            break
        total = payload.get("total")
        if isinstance(total, int) and start_at + len(page) >= total:
            break
        if len(page) < page_size:
            break
        start_at += len(page)

    return [_normalize_issue(issue) for issue in issues[:target_limit]]


def get_issue_raw(issue_key):
    """Fetch the full raw issue JSON directly from Jira's REST API —
    bypassing mcp-atlassian's curated field subset — so custom fields
    (Complexity, RAG Status, etc.) are available for compliance checks.
    Raises on missing credentials or a failed request."""
    jira_url, jira_username, auth_secret = _get_jira_auth_config()

    if not all([jira_url, jira_username, auth_secret]):
        raise RuntimeError(
            "JIRA_URL, JIRA_USERNAME, and one of JIRA_PASSWORD/JIRA_API_TOKEN "
            "must be set for direct Jira field access."
        )
    url = f"{jira_url.rstrip('/')}/rest/api/2/issue/{issue_key}"
    #url = f"https://jira.bics-collaboration.homeoffice.gov.uk/rest/api/2/issue/{issue_key}"


    print(f"Fetching Jira issue {issue_key} from {url}...")
    resp = requests.get(url, auth=HTTPBasicAuth(jira_username, auth_secret), timeout=30)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        if resp.status_code in (401, 403):
            raise RuntimeError(
                "Jira authentication failed while fetching issue data. "
                "Verify JIRA_USERNAME and JIRA_PASSWORD/JIRA_API_TOKEN, then restart the app."
            ) from exc
        raise
    return resp.json()


def check_jira_auth(timeout=15):
    """Validate Jira credentials by calling /rest/api/2/myself.

    Returns a dict with ok/status_code/message and optional account fields.
    """
    jira_url, jira_username, auth_secret = _get_jira_auth_config()
    if not all([jira_url, jira_username, auth_secret]):
        return {
            "ok": False,
            "status_code": 0,
            "message": "Missing JIRA_URL/JIRA_USERNAME or JIRA_PASSWORD/JIRA_API_TOKEN",
        }
    url = f"{jira_url.rstrip('/')}/rest/api/2/myself"
    try:
        resp = requests.get(url, auth=HTTPBasicAuth(jira_username, auth_secret), timeout=timeout)
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status_code": 0,
            "message": f"Network/SSL error contacting Jira: {exc}",
        }

    if resp.ok:
        payload = resp.json() if resp.content else {}
        return {
            "ok": True,
            "status_code": resp.status_code,
            "message": "Jira authentication successful",
            "displayName": payload.get("displayName"),
            "accountId": payload.get("accountId"),
        }

    return {
        "ok": False,
        "status_code": resp.status_code,
        "message": "Jira authentication failed",
        "response": resp.text[:500],
    }

