import os
import requests
from mcp.server.fastmcp import FastMCP

# ============================================================
# Configuration
# ============================================================

JIRA_URL = os.getenv("JIRA_URL", "https://jira.bics-collaboration.homeoffice.gov.uk")
JIRA_USERNAME = os.getenv("JIRA_USERNAME", "prajakta.chavan")
JIRA_PASSWORD =  "Mastek$2027"

# Jira issue to fetch
ISSUE_KEY = os.getenv("ISSUE_KEY", "PROJ-123")

# ============================================================
# MCP Server
# ============================================================

mcp = FastMCP("jira-mcp-server")


# ============================================================
# Jira API Helper
# ============================================================

def get_jira_issue(issue_key: str):
    """
    Fetch a single Jira issue using Jira REST API.
    """

    url = f"{JIRA_URL}/rest/api/2/issue/{issue_key}"

    response = requests.get(
        url,
        auth=(JIRA_USERNAME, JIRA_PASSWORD),
        headers={
            "Accept": "application/json"
        },
        timeout=30
    )

    if response.status_code != 200:
        raise Exception(
            f"Jira API failed: {response.status_code} - {response.text}"
        )

    return response.json()


# ============================================================
# MCP Tool
# ============================================================

@mcp.tool()
def fetch_jira_issue(issue_key: str) -> dict:
    """
    Fetch a single Jira issue.
    """

    issue = get_jira_issue(issue_key)

    fields = issue.get("fields", {})

    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": fields.get("status", {}).get("name"),
        "priority": (
            fields.get("priority", {}).get("name")
            if fields.get("priority")
            else None
        ),
        "assignee": (
            fields.get("assignee", {}).get("displayName")
            if fields.get("assignee")
            else None
        ),
        "reporter": (
            fields.get("reporter", {}).get("displayName")
            if fields.get("reporter")
            else None
        ),
        "description": fields.get("description"),
    }


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    print("Connecting to Jira...")
    print(f"JIRA URL: {JIRA_URL}")
    print(f"Issue: {ISSUE_KEY}")

    try:
        issue = get_jira_issue(ISSUE_KEY)

        fields = issue.get("fields", {})

        print("\n====================================")
        print("JIRA ISSUE")
        print("====================================")
        print(f"Key       : {issue.get('key')}")
        print(f"Summary   : {fields.get('summary')}")
        print(
            f"Status    : "
            f"{fields.get('status', {}).get('name')}"
        )
        print(
            f"Priority  : "
            f"{fields.get('priority', {}).get('name') if fields.get('priority') else 'None'}"
        )
        print(
            f"Assignee  : "
            f"{fields.get('assignee', {}).get('displayName') if fields.get('assignee') else 'Unassigned'}"
        )
        print("====================================")

    except Exception as e:
        print(f"\nERROR: {e}")

    # Start MCP server
    mcp.run(transport="sse")