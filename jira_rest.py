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

JIRA_URL = os.environ.get("JIRA_URL")
JIRA_USERNAME = os.environ.get("JIRA_USERNAME")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")


def get_issue_raw(issue_key):
    """Fetch the full raw issue JSON directly from Jira's REST API —
    bypassing mcp-atlassian's curated field subset — so custom fields
    (Complexity, RAG Status, etc.) are available for compliance checks.
    Raises on missing credentials or a failed request."""
    if not all([JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN]):
        raise RuntimeError(
            "JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN must be set in the Flask "
            "app's own environment (not just jira.env for the Docker container) "
            "for direct field access to work."
        )
    url = f"{JIRA_URL.rstrip('/')}/rest/api/3/issue/{issue_key}"
    resp = requests.get(url, auth=HTTPBasicAuth(JIRA_USERNAME, JIRA_API_TOKEN), timeout=30)
    resp.raise_for_status()
    return resp.json()
