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
