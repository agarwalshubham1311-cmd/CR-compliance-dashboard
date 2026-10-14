#!/usr/bin/env python
"""
List all available epics in JIRA.
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

def list_all_epics(jira_url, jira_username, jira_password):
    """Search for all epics."""

    jql = 'type = Epic'

    url = f"{jira_url.rstrip('/')}/rest/api/2/search"

    params = {
        "jql": jql,
        "maxResults": 100,
        "fields": "key,summary,status,assignee,created,updated"
    }

    try:
        print(f"🔍 Searching for all epics...")
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
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
            print(f"   Response: {e.response.text}")
        sys.exit(1)

def display_epics(data):
    """Display the epics in a formatted way."""
    issues = data.get("issues", [])
    total = data.get("total", 0)

    if not issues:
        print(f"\n✓ Found {total} epics")
        return

    print(f"\n✓ Found {total} epic(s):\n")
    print("=" * 100)
    print(f"{'Key':<15} {'Status':<15} {'Summary':<60} {'Assignee':<20}")
    print("=" * 100)

    for issue in issues:
        fields = issue.get("fields", {})
        key = issue.get("key", "N/A")
        status = fields.get("status", {}).get("name", "N/A")
        summary = fields.get("summary", "N/A")[:60]
        assignee = fields.get("assignee")
        assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"

        print(f"{key:<15} {status:<15} {summary:<60} {assignee_name:<20}")

    print("=" * 100)

    # Print detailed view
    print("\n" + "=" * 100)
    print("DETAILED VIEW:")
    print("=" * 100)

    for i, issue in enumerate(issues, 1):
        fields = issue.get("fields", {})
        key = issue.get("key", "N/A")
        summary = fields.get("summary", "N/A")
        status = fields.get("status", {}).get("name", "N/A")
        assignee = fields.get("assignee")
        assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
        created = fields.get("created", "N/A")
        updated = fields.get("updated", "N/A")

        print(f"\n{i}. [{key}] {summary}")
        print(f"   Status: {status}")
        print(f"   Assignee: {assignee_name}")
        print(f"   Created: {created}")
        print(f"   Updated: {updated}")

def main():
    """Main entry point."""
    print(f"🚀 JIRA Compliance Dashboard - Epic Lister\n")

    # Get credentials
    jira_url, jira_username, jira_password = get_jira_credentials()
    print(f"✓ Connected to: {jira_url}\n")

    # Search for epics
    data = list_all_epics(jira_url, jira_username, jira_password)

    # Display results
    display_epics(data)

if __name__ == "__main__":
    main()

