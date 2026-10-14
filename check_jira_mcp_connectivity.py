import argparse
import asyncio
import json
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth

from mcp import ClientSession
from mcp.client.sse import sse_client


DEFAULT_JQL = "project IS NOT EMPTY ORDER BY created DESC"
DEFAULT_MCP_URL = "http://localhost:8000/sse"
DEFAULT_ENV_FILE = str(Path(__file__).with_name("jira.env"))


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    details: Any | None = None


def _mask(value: str | None) -> str:
    if not value:
        return "<missing>"
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}***{value[-2:]}"


def _load_env_file(env_file: str | None) -> list[str]:
    """Load KEY=VALUE pairs from a dotenv-style file without shell expansion.

    Existing environment variables are preserved so shell-exported values still
    win over jira.env defaults.
    """
    if not env_file:
        return []

    path = Path(env_file)
    if not path.is_file():
        return []

    loaded_keys: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("export "):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value
        loaded_keys.append(key)
    return loaded_keys


def _host_port_from_url(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    if parsed.port:
        return host, parsed.port
    return host, 443 if parsed.scheme == "https" else 80


def _tcp_reachable(url: str, timeout_seconds: float = 5.0) -> bool:
    host, port = _host_port_from_url(url)
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


async def _list_tools(mcp_url: str) -> list[dict[str, Any]]:
    async with sse_client(mcp_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [tool.model_dump() for tool in tools.tools]


async def _jira_search(mcp_url: str, jql: str) -> dict[str, Any]:
    async with sse_client(mcp_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("jira_search", {"jql": jql})
            payload: dict[str, Any] = {"content": [block.model_dump() for block in result.content]}
            return payload


def _parse_jira_search_result(result: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    for block in result.get("content", []):
        text = block.get("text")
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        issues = parsed.get("issues", [])
        break
    return len(issues), issues


def _direct_jira_me(jira_url: str, username: str, password: str) -> requests.Response:
    base = jira_url.rstrip("/")
    url = f"{base}/rest/api/2/myself"
    return requests.get(url, auth=HTTPBasicAuth(username, password), timeout=30)


def run_checks(mcp_url: str, jira_url: str, username: str, password: str, jql: str) -> list[CheckResult]:
    results: list[CheckResult] = []

    results.append(
        CheckResult(
            name="env",
            ok=all([mcp_url, jira_url, username, password]),
            message=(
                f"MCP={mcp_url!r}, JIRA_URL={jira_url!r}, "
                f"JIRA_USERNAME={_mask(username)}, JIRA_SECRET={_mask(password)}"
            ),
        )
    )

    if not _tcp_reachable(mcp_url):
        results.append(CheckResult("mcp_tcp", False, f"Cannot reach MCP endpoint over TCP: {mcp_url}"))
        return results
    results.append(CheckResult("mcp_tcp", True, f"TCP reachable: {mcp_url}"))

    try:
        tools = asyncio.run(_list_tools(mcp_url))
        results.append(CheckResult("mcp_list_tools", True, f"MCP tool discovery succeeded ({len(tools)} tools)", tools))
    except Exception as exc:
        results.append(CheckResult("mcp_list_tools", False, f"Failed to list MCP tools: {exc}"))
        return results

    try:
        search_result = asyncio.run(_jira_search(mcp_url, jql))
        count, issues = _parse_jira_search_result(search_result)
        results.append(
            CheckResult(
                name="mcp_jira_search",
                ok=True,
                message=f"jira_search succeeded with JQL={jql!r} and returned {count} issues",
                details=issues[:5],
            )
        )
    except Exception as exc:
        results.append(CheckResult("mcp_jira_search", False, f"jira_search failed: {exc}"))

    try:
        resp = _direct_jira_me(jira_url, username, password)
        if resp.ok:
            try:
                me = resp.json()
            except Exception:
                me = resp.text
            results.append(CheckResult("jira_direct_auth", True, "Direct Jira auth succeeded via /rest/api/2/myself", me))
        else:
            results.append(
                CheckResult(
                    "jira_direct_auth",
                    False,
                    f"Direct Jira auth failed: HTTP {resp.status_code}",
                    resp.text,
                )
            )
    except Exception as exc:
        results.append(CheckResult("jira_direct_auth", False, f"Direct Jira auth request failed: {exc}"))

    return results


def print_results(results: list[CheckResult]) -> int:
    print("=== Jira MCP Connectivity Check ===")
    failed = False
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.message}")
        if result.details is not None and result.ok:
            print(f"       details: {json.dumps(result.details, indent=2, ensure_ascii=False)}")
        if not result.ok:
            failed = True
    print("=== End Check ===")
    return 1 if failed else 0


def main() -> int:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument(
        "--env-file",
        default=os.environ.get("JIRA_ENV_FILE", DEFAULT_ENV_FILE),
        help="Dotenv file to load before reading Jira/MCP settings",
    )
    pre_args, remaining_argv = pre_parser.parse_known_args()
    loaded_keys = _load_env_file(pre_args.env_file)

    parser = argparse.ArgumentParser(
        description="Check Jira connectivity through the MCP server using JIRA username and password/API token."
    )
    parser.add_argument(
        "--env-file",
        default=pre_args.env_file,
        help="Dotenv file loaded before reading Jira/MCP settings",
    )
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("MCP_SERVER_URL", DEFAULT_MCP_URL),
        help=f"MCP SSE endpoint (default: env MCP_SERVER_URL or {DEFAULT_MCP_URL})",
    )
    parser.add_argument(
        "--jira-url",
        default=os.environ.get("JIRA_URL", ""),
        help="Base Jira URL, e.g. https://your-jira.example.com/",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("JIRA_USERNAME", ""),
        help="Jira username",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("JIRA_PASSWORD", "") or os.environ.get("JIRA_API_TOKEN", ""),
        help="Jira password or API token",
    )
    parser.add_argument(
        "--jql",
        default=os.environ.get("JIRA_CONNECTIVITY_JQL", DEFAULT_JQL),
        help=f"JQL used for the MCP jira_search check (default: {DEFAULT_JQL!r})",
    )
    args = parser.parse_args(remaining_argv)

    results = run_checks(args.mcp_url, args.jira_url, args.username, args.password, args.jql)
    if loaded_keys:
        results.insert(
            0,
            CheckResult(
                name="env_file",
                ok=True,
                message=f"Loaded {len(loaded_keys)} variables from {pre_args.env_file}",
                details=loaded_keys,
            ),
        )
    elif pre_args.env_file and Path(pre_args.env_file).is_file():
        results.insert(
            0,
            CheckResult(
                name="env_file",
                ok=True,
                message=f"Loaded environment file {pre_args.env_file}",
            ),
        )
    return print_results(results)


if __name__ == "__main__":
    sys.exit(main())

