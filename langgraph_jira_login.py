"""
Jira Login Validation Workflow
Provides simple, direct validation for Jira Cloud and Data Center authentication
without using LangGraph (which has deep regex recursion issues in pydantic)
"""

import json
import requests
from requests.auth import HTTPBasicAuth
import logging

logger = logging.getLogger(__name__)

# ============================================================================
# Validation Functions
# ============================================================================
def validate_jira_url(url: str) -> dict:
    """Validate if the provided Jira URL is accessible and determine Jira type"""
    try:
        # Remove trailing slash
        url = url.rstrip('/')

        # Try to fetch Jira server info first (Data Center / Server)
        info_url = f"{url}/rest/api/2/serverInfo"
        headers = {'User-Agent': 'Jira-Agent/1.0'}

        resp = requests.get(info_url, headers=headers, timeout=5)

        if resp.status_code == 200:
            data = resp.json()
            return {
                "valid": True,
                "url": url,
                "version": data.get("version", "unknown"),
                "deployment_type": data.get("deploymentType", "server"),
                "message": f"✓ Valid Jira instance detected: {data.get('version')}"
            }

        # Cloud often returns 404 on /serverInfo, so try the Cloud endpoint.
        cloud_url = f"{url}/rest/api/3/myself"
        cloud_resp = requests.get(cloud_url, headers=headers, timeout=5)

        if cloud_resp.status_code in (200, 401, 403):
            return {
                "valid": True,
                "url": url,
                "version": None,
                "deployment_type": "cloud",
                "message": "✓ Valid Jira Cloud instance detected"
            }

        return {
            "valid": False,
            "url": url,
            "message": f"✗ URL returned status {resp.status_code} (serverInfo), cloud fallback {cloud_resp.status_code}",
            "error": cloud_resp.text[:200] if cloud_resp.text else resp.text[:200]
        }
    except requests.exceptions.Timeout:
        return {
            "valid": False,
            "url": url,
            "message": "✗ URL timeout - server not responding",
            "error": "Connection timeout"
        }
    except Exception as e:
        return {
            "valid": False,
            "url": url,
            "message": f"✗ Error validating URL: {str(e)}",
            "error": str(e)
        }

def validate_datacenter_credentials(url: str, username: str, password: str) -> dict:
    """Validate Jira Data Center credentials using Basic Auth"""
    try:
        url = url.rstrip('/')
        auth = HTTPBasicAuth(username, password)

        # Test with get_issue endpoint
        test_url = f"{url}/rest/api/2/myself"
        resp = requests.get(test_url, auth=auth, timeout=5)

        if resp.status_code == 200:
            user_data = resp.json()
            return {
                "valid": True,
                "username": user_data.get("name"),
                "display_name": user_data.get("displayName"),
                "email": user_data.get("emailAddress"),
                "message": f"✓ Authentication successful for {user_data.get('displayName')}"
            }
        elif resp.status_code == 401:
            return {
                "valid": False,
                "message": "✗ Authentication failed - Invalid credentials",
                "error": "401 Unauthorized"
            }
        else:
            return {
                "valid": False,
                "message": f"✗ Failed to authenticate (HTTP {resp.status_code})",
                "error": resp.text[:200]
            }
    except Exception as e:
        return {
            "valid": False,
            "message": f"✗ Error during authentication: {str(e)}",
            "error": str(e)
        }

def validate_cloud_credentials(url: str, username: str, api_token: str) -> dict:
    """Validate Jira Cloud credentials using API Token"""
    try:
        url = url.rstrip('/')
        auth = HTTPBasicAuth(username, api_token)

        # Cloud-specific endpoint
        test_url = f"{url}/rest/api/3/myself"
        headers = {'Accept': 'application/json'}
        resp = requests.get(test_url, auth=auth, headers=headers, timeout=5)

        if resp.status_code == 200:
            user_data = resp.json()
            return {
                "valid": True,
                "account_id": user_data.get("accountId"),
                "display_name": user_data.get("displayName"),
                "email": user_data.get("emailAddress"),
                "message": f"✓ Cloud authentication successful for {user_data.get('displayName')}"
            }
        elif resp.status_code == 401:
            return {
                "valid": False,
                "message": "✗ Token validation failed - Check your API token",
                "error": "401 Unauthorized"
            }
        else:
            return {
                "valid": False,
                "message": f"✗ Failed to validate token (HTTP {resp.status_code})",
                "error": resp.text[:200]
            }
    except Exception as e:
        return {
            "valid": False,
            "message": f"✗ Error during token validation: {str(e)}",
            "error": str(e)
        }
def determine_jira_type(url: str) -> dict:
    """Determine if Jira instance is Cloud or Data Center"""
    try:
        url = url.rstrip('/')
        info_url = f"{url}/rest/api/2/serverInfo"

        resp = requests.get(info_url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            deployment_type = data.get("deploymentType", "server").lower()

            # Cloud instances will fail /rest/api/2/serverInfo but succeed on /rest/api/3
            if deployment_type == "cloud":
                return {"type": "cloud", "message": "Detected: Jira Cloud"}
            else:
                return {"type": "datacenter", "message": "Detected: Jira Data Center"}
        else:
            # Try Cloud endpoint
            cloud_url = f"{url}/rest/api/3/myself"
            cloud_resp = requests.get(cloud_url, timeout=5)
            if cloud_resp.status_code in [200, 401, 403]:
                return {"type": "cloud", "message": "Detected: Jira Cloud"}
            else:
                return {"type": "datacenter", "message": "Assuming: Jira Data Center"}
    except Exception as e:
        return {"type": "unknown", "message": f"Could not determine type: {str(e)}"}
def handle_jira_login(jira_url: str, username: str, password_or_token: str, jira_type: str = "") -> dict:
    """
    Main function to handle Jira login with agentic workflow

    Args:
        jira_url: Jira instance URL
        username: Jira username
        password_or_token: Password (Data Center) or API Token (Cloud)
        jira_type: 'cloud', 'datacenter', or '' for auto-detection

    Returns:
        dict with login result and status
    """
    messages = ["🚀 Starting Jira login workflow..."]
    is_valid = False
    error_message = ""
    validation_details = {}

    try:
        # Step 1: Validate URL
        messages.append("Step 1 - URL Validation: Checking Jira URL...")
        url_result = validate_jira_url(jira_url)
        messages.append(f"Step 1 - URL Validation: {url_result['message']}")
        validation_details = url_result

        if not url_result.get("valid"):
            error_message = url_result.get("error", "Invalid URL")
            return {
                "success": False,
                "jira_type": jira_type,
                "jira_url": jira_url,
                "username": username,
                "messages": messages,
                "error": error_message,
                "validation_details": validation_details
            }

        # Step 2: Determine Jira type if not provided
        if not jira_type:
            messages.append("Step 2 - Type Detection: Auto-detecting Jira type...")
            type_result = determine_jira_type(jira_url)
            jira_type = type_result["type"]
            messages.append(f"Step 2 - Type Detection: {type_result['message']}")
        else:
            messages.append(f"Step 2 - Type Detection: Using provided type '{jira_type}'")

        # Step 3: Validate credentials
        messages.append("Step 3 - Credential Validation: Checking credentials...")
        if jira_type == "cloud":
            cred_result = validate_cloud_credentials(jira_url, username, password_or_token)
        else:
            cred_result = validate_datacenter_credentials(jira_url, username, password_or_token)

        messages.append(f"Step 3 - Credential Validation: {cred_result['message']}")
        validation_details.update(cred_result)

        if not cred_result.get("valid"):
            error_message = cred_result.get("error", "Authentication failed")
            return {
                "success": False,
                "jira_type": jira_type,
                "jira_url": jira_url,
                "username": username,
                "messages": messages,
                "error": error_message,
                "validation_details": validation_details
            }

        # Step 4: Success
        is_valid = True
        messages.append("Step 4 - Credentials validated and ready for storage")
        logger.info(f"Jira login successful: {username} on {jira_type}")

        return {
            "success": is_valid,
            "jira_type": jira_type,
            "jira_url": jira_url,
            "username": username,
            "messages": messages,
            "error": error_message,
            "validation_details": validation_details
        }
    except Exception as e:
        logger.exception(f"Error in Jira login workflow: {e}")
        return {
            "success": False,
            "jira_type": jira_type,
            "jira_url": jira_url,
            "username": username,
            "messages": ["❌ Workflow execution failed"],
            "error": str(e),
            "validation_details": {}
        }
