#!/usr/bin/env python
"""
Display all JIRA tickets that belong to a specific epic.
Usage: python show_epic_issues.py [epic_name]
Defaults to "EPIC" if no epic name provided.
"""

import os
import sys
import requests
import json
from requests.auth import HTTPBasicAuth
from pathlib import Path
import tempfile
import certifi

# Configure CA bundle similar to jira_rest.py
def configure_ca_bundle():
    if os.environ.get("REQUESTS_CA_BUNDLE"):
        return

    if sys.platform == "win32":
        base_bundle = Path(certifi.where())
        corporate_ca = Path(__file__).parent / "corporate-ca.crt"

        if corporate_ca.exists():
            merged_bundle = Path(tempfile.gettempdir()) / "cr-compliance-requests-ca-bundle.pem"
            try:
                merged_bundle.write_text(
                    base_bundle.read_text(encoding="utf-8") + "\n" + corporate_ca.read_text(encoding="utf-8"),
                    encoding="utf-8"
                )
                os.environ["REQUESTS_CA_BUNDLE"] = str(merged_bundle)
                print(f"✓ Using merged CA bundle for requests")
                return
            except Exception as e:
                print(f"⚠ Could not build merged CA bundle ({e}); falling back to certifi bundle")

        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
        return

    os.environ.setdefault("REQUESTS_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")

configure_ca_bundle()

def get_jira_credentials():
    """Get JIRA credentials from environment variables."""
    jira_url = os.environ.get("JIRA_URL")
    jira_username = os.environ.get("JIRA_USERNAME")
    jira_password = os.environ.get("JIRA_PASSWORD")

    if not all([jira_url, jira_username, jira_password]):
        print("❌ Error: JIRA credentials not found in environment variables.")
        print("   Please set JIRA_URL, JIRA_USERNAME, and JIRA_PASSWORD")
        sys.exit(1)

    return jira_url, jira_username, jira_password

def search_epic_issues(epic_name, jira_url, jira_username, jira_password):
    """Search for all issues in a specific epic."""

    # Construct JQL query to find issues in the epic
    # This uses either 'parent' (for child issues) or 'epic' field
    jql = f'(parent = "{epic_name}" OR "Epic Link" = "{epic_name}")'

    url = f"{jira_url.rstrip('/')}/rest/api/2/search"

    params = {
        "jql": jql,
        "maxResults": 100,
        "fields": "key,summary,status,issuetype,assignee,priority,parent"
    }

    try:
        print(f"🔍 Searching for issues in epic '{epic_name}'...")
        response = requests.get(
            url,
            params=params,
            auth=HTTPBasicAuth(jira_username, jira_password),
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Error querying JIRA: {e}")
        if hasattr(e.response, 'text'):
            print(f"   Response: {e.response.text}")
        sys.exit(1)

def display_issues(data, epic_name):
    """Display the issues in a formatted way."""
    issues = data.get("issues", [])
    total = data.get("total", 0)

    if not issues:
        print(f"\n✓ Found {total} issues in epic '{epic_name}' (none displayed)")
        return

    print(f"\n✓ Found {total} issues in epic '{epic_name}':\n")
    print("=" * 120)
    print(f"{'Key':<12} {'Type':<10} {'Status':<15} {'Summary':<50} {'Assignee':<20}")
    print("=" * 120)

    for issue in issues:
        fields = issue.get("fields", {})
        key = issue.get("key", "N/A")
        issue_type = fields.get("issuetype", {}).get("name", "N/A")
        status = fields.get("status", {}).get("name", "N/A")
        summary = fields.get("summary", "N/A")[:50]
        assignee = fields.get("assignee")
        assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"

        print(f"{key:<12} {issue_type:<10} {status:<15} {summary:<50} {assignee_name:<20}")

    print("=" * 120)
    print(f"\nTotal: {total} issue(s)")

    # Print detailed view
    print("\n" + "=" * 120)
    print("DETAILED VIEW:")
    print("=" * 120)

    for i, issue in enumerate(issues, 1):
        fields = issue.get("fields", {})
        key = issue.get("key", "N/A")
        summary = fields.get("summary", "N/A")
        status = fields.get("status", {}).get("name", "N/A")
        issue_type = fields.get("issuetype", {}).get("name", "N/A")
        assignee = fields.get("assignee")
        assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
        priority = fields.get("priority", {}).get("name", "N/A")
        parent = fields.get("parent", {})
        parent_key = parent.get("key", "N/A") if parent else "N/A"

        print(f"\n{i}. [{key}] {summary}")
        print(f"   Type: {issue_type}")
        print(f"   Status: {status}")
        print(f"   Assignee: {assignee_name}")
        print(f"   Priority: {priority}")
        print(f"   Parent: {parent_key}")

def main():
    """Main entry point."""
    # Get epic name from command line or use default
    epic_name = sys.argv[1] if len(sys.argv) > 1 else "EPIC"

    print(f"🚀 JIRA Compliance Dashboard - Epic Issue Viewer\n")

    # Get credentials
    jira_url, jira_username, jira_password = get_jira_credentials()
    print(f"✓ Connected to: {jira_url}")

    # Search for issues
    data = search_epic_issues(epic_name, jira_url, jira_username, jira_password)

    # Display results
    display_issues(data, epic_name)

if __name__ == "__main__":
    main()

