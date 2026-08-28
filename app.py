import asyncio
import logging
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory
from mcp import ClientSession
from mcp.client.sse import sse_client


def _load_local_env_file(env_file_name: str = "jira.env"):
    """Load dotenv-style values for local runs (Docker already injects env)."""
    env_path = Path(__file__).with_name(env_file_name)
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


_load_local_env_file()

app = Flask(__name__, static_folder="static", static_url_path="")
logger = logging.getLogger("compliance.scope")

# Keep logging lightweight and opt-in via LOG_LEVEL/COMPLIANCE_SCOPE_LOG_LEVEL.
if not logging.getLogger().handlers:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger.setLevel(os.environ.get("COMPLIANCE_SCOPE_LOG_LEVEL", os.environ.get("LOG_LEVEL", "INFO")).upper())

# URL of the jira-mcp container's SSE endpoint (set MCP_SERVER_URL env var to override)
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/sse")

DEFAULT_CR_DISCOVERY_JQL = 'labels = "CR"'
CR_DISCOVERY_JQL = os.environ.get("CR_DISCOVERY_JQL", DEFAULT_CR_DISCOVERY_JQL)
DEFAULT_PROJECT_KEY = os.environ.get("DEFAULT_PROJECT_KEY", "ACC").strip().upper()
ASSIGNEE_SCOPE_CACHE_TTL_SEC = int(os.environ.get("ASSIGNEE_SCOPE_CACHE_TTL_SEC", "180"))
_ASSIGNEE_SCOPE_CACHE = {}
SCOPED_SCAN_MAX_CRS = int(os.environ.get("SCOPED_SCAN_MAX_CRS", "150"))
ASSIGNEE_DISCOVERY_MAX_ISSUES = int(os.environ.get("ASSIGNEE_DISCOVERY_MAX_ISSUES", "600"))

import db as compliance_db
import compliance as compliance_rules
import ai_classify
import ai_draft
import field_rules
import jira_rest

compliance_db.init_db()

# Load any previously AI-learned (or human-confirmed) status mappings so
# restarts don't lose them and don't re-ask the LLM for known statuses.
for m in compliance_db.get_all_status_mappings():
    compliance_rules.learn_alias(m["status_type"], m["raw_status"], m["target_status"])


# ---------------------------------------------------------------------------
# Compliance mapping derived from the ACC: Change Request Workflow and
# ACC Story Workflow diagrams. Keys/values are normalized to UPPERCASE for
# comparison since Jira's actual status.name casing varies (e.g. "In
# development" vs the workflow diagram's "IN DEVELOPMENT").
#
# For each CR phase, this lists every Story status considered compliant
# (i.e. the story hasn't gotten ahead of or fallen behind the CR's phase).
# Any story status NOT in the list for the CR's current status is flagged
# as non-compliant — a single flat signal, no ahead/behind distinction.
# ---------------------------------------------------------------------------
CR_ALLOWED_STORY_STATUSES = {
    "NEW REQUEST": ["BACKLOG"],
    "IMPACT ASSESSMENT REVIEW/APPROVAL": ["BACKLOG"],
    "IMPACT ASSESSMENT APPROVED": ["BACKLOG"],
    "IMPACT ASSESSMENT IN PROGRESS": ["BACKLOG"],
    "BUILD REVIEW / APPROVAL": ["BACKLOG"],
    "CR AGREED": ["BACKLOG"],
    "STORIES CREATED": ["BACKLOG", "IN ANALYSIS"],
    "IN DEVELOPMENT": [
        "IN ANALYSIS", "ANALYSIS DONE", "IN PROGRESS",
        "DEVELOPMENT DONE", "TEST (DEV)", "DEVTEST COMPLETE",
    ],
    "READY FOR MERGE": ["READY FOR MERGE"],
    "IN FEATURE ENV": ["IN FEATURE ENVIRONMENT", "AUTOSIT"],
    "IN PRP": ["READY FOR PRP TEST", "IN PRP TEST"],
    "READY FOR PRODUCTION": ["READY FOR PROD"],
    "DELIVERY COMPLETE": ["LIVE - FEATURE SWITCHED ON", "LIVE - FEATURE SWITCHED OFF"],
    "BLOCKED": ["BLOCKED"],
    "WITHDRAWN": ["WITHDRAWN"],
}


def is_compliant(cr_status, story_status):
    """Returns (compliant: bool, reason: str). Falls back to strict
    equality with a flagged note if the CR status isn't in our mapping
    (e.g. a workflow status added later that we haven't mapped yet)."""
    if not cr_status or not story_status:
        return False, "missing_status"

    cr_key = cr_status.strip().upper()
    story_key = story_status.strip().upper()

    if cr_key not in CR_ALLOWED_STORY_STATUSES:
        return (story_key == cr_key), "unmapped_cr_status"

    allowed = CR_ALLOWED_STORY_STATUSES[cr_key]
    return (story_key in allowed), "phase_mapping"


def _jira_browse_base():
    jira_url = (os.environ.get("JIRA_URL") or "").strip().rstrip("/")
    return f"{jira_url}/browse/" if jira_url else ""


def _severity_rank(severity):
    return {"Critical": 3, "High": 2, "Medium": 1, "Low": 0, "OK": -1}.get(severity, -1)


def _assignee_name(issue):
    assignee = (issue or {}).get("assignee") or {}
    return (assignee.get("displayName") or assignee.get("name") or "").strip()


def _normalize_assignee(value):
    return (value or "").strip().lower()


def _effective_project_key(project_key: str | None, board_id: str | None):
    """When a board is selected, board scope takes precedence over project scope."""
    return None if board_id else project_key


def _issue_type_name(issue):
    return (((issue or {}).get("issue_type") or {}).get("name") or "").strip().upper()


def _is_story_issue(issue):
    issue_type = _issue_type_name(issue)
    return issue_type == "STORY" or issue_type == "USER STORY" or issue_type.endswith(" STORY")


def _is_cr_issue(issue):
    issue_type = _issue_type_name(issue)
    return issue_type in {"CR", "CHANGE REQUEST"}


def _has_cr_label(issue):
    labels = (issue or {}).get("labels") or []
    return any(str(label).strip().upper() == "CR" for label in labels)


def _filter_active_rows(active_rows, scoped_cr_keys, board_story_keys=None):
    total_active_rows = len(active_rows or [])
    cr_scope_count = len(scoped_cr_keys or set())
    board_story_count = len(board_story_keys) if board_story_keys is not None else None

    rows = [row for row in active_rows if row.get("cr_key") in scoped_cr_keys]
    rows_after_cr_scope = len(rows)
    if board_story_keys is not None:
        rows = [row for row in rows if row.get("story_key") in board_story_keys]
    rows_after_board_scope = len(rows)
    rows_after_scope = len(rows)

    logger.info(
        "[scope_filter] active=%s cr_scope=%s board_story_scope=%s -> after_cr=%s after_board=%s",
        total_active_rows,
        cr_scope_count,
        board_story_count if board_story_count is not None else "-",
        rows_after_cr_scope,
        rows_after_board_scope,
    )

    # This warning pinpoints the exact filter stage that collapsed the result to zero.
    if total_active_rows > 0 and rows_after_scope == 0:
        logger.warning(
            "[scope_filter_zero] Result became zero after filtering. details={active:%s, cr_scope:%s, board_story_scope:%s, after_cr:%s, after_board:%s}",
            total_active_rows,
            cr_scope_count,
            board_story_count if board_story_count is not None else "-",
            rows_after_cr_scope,
            rows_after_board_scope,
        )
    return rows


def _parse_first_json_block(content_blocks):
    import json as _json

    for block in content_blocks:
        text = block.get("text")
        if not text:
            continue
        try:
            return _json.loads(text)
        except Exception:
            continue
    return None


def _parse_issues_from_content(content_blocks):
    parsed = _parse_first_json_block(content_blocks)
    if isinstance(parsed, dict):
        issues = parsed.get("issues")
        if isinstance(issues, list):
            return issues
    return []


async def _jira_search_issues(session, jql: str):
    result = await session.call_tool("jira_search", {"jql": jql})
    return _parse_issues_from_content([block.model_dump() for block in result.content])


async def _search_issues_with_fallback(jql: str, session=None, max_results: int = 200):
    mcp_error = None
    if session is not None:
        try:
            return await _jira_search_issues(session, jql)
        except Exception as exc:
            mcp_error = exc
            print(f"[jira_search] MCP search failed for JQL {jql!r}; falling back to direct Jira REST: {exc}")

    try:
        return await asyncio.to_thread(jira_rest.search_issues, jql, max_results)
    except Exception:
        if mcp_error is not None:
            raise mcp_error
        raise


def _scope_jql(jql: str, project_key: str | None = None):
    if not project_key:
        return jql
    project = project_key.strip().upper()
    if not project:
        return jql
    return f'(project = "{project}") AND ({jql})'


def _cr_discovery_jql_candidates(project_key: str | None = None):
    project_key = _effective_project_key(project_key, None)
    custom_jql_configured = "CR_DISCOVERY_JQL" in os.environ
    candidate_jqls = [_scope_jql(CR_DISCOVERY_JQL, project_key)]
    if not custom_jql_configured:
        candidate_jqls.extend([
            _scope_jql('issuetype = "Change Request"', project_key),
            _scope_jql('issuetype = "CR"', project_key),
        ])
    return candidate_jqls, custom_jql_configured


async def _discover_crs(session, project_key: str | None = None, board_id: str | None = None):
    project_key = _effective_project_key(project_key, board_id)
    candidate_jqls, custom_jql_configured = _cr_discovery_jql_candidates(project_key)

    if board_id:
        board_last_error = None
        for jql in candidate_jqls:
            try:
                board_issues = await asyncio.to_thread(jira_rest.search_board_issues, board_id, jql, 500)
                if board_issues:
                    return board_issues, f'board = {board_id} AND ({jql})'
                if custom_jql_configured and jql == _scope_jql(CR_DISCOVERY_JQL, project_key):
                    return [], f'board = {board_id} AND ({jql})'
            except Exception as exc:
                board_last_error = exc
                continue
        if board_last_error:
            print(f"[cr_discovery] board-scoped search failed for board {board_id}: {board_last_error}; falling back to JQL")

    seen = set()
    last_error = None
    for jql in candidate_jqls:
        if jql in seen:
            continue
        seen.add(jql)
        try:
            issues = await _search_issues_with_fallback(jql, session=session)
            if issues:
                if jql != CR_DISCOVERY_JQL:
                    print(f"[cr_discovery] using fallback JQL: {jql}")
                return issues, jql
            if custom_jql_configured and jql == _scope_jql(CR_DISCOVERY_JQL, project_key):
                return [], jql
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    return [], _scope_jql(CR_DISCOVERY_JQL, project_key)


async def _discover_crs_for_scope(project_key: str | None = None, board_id: str | None = None):
    """Resolve CR keys for a project/board scope, with MCP then direct REST fallback."""
    project_key = _effective_project_key(project_key, board_id)
    try:
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                crs, used_jql = await _discover_crs(session, project_key=project_key, board_id=board_id)
                logger.info(
                    "[scope_cr_discovery] source=mcp project=%s board=%s cr_count=%s jql=%r",
                    project_key or "-",
                    board_id or "-",
                    len(crs or []),
                    used_jql,
                )
                return crs, used_jql
    except Exception as exc:
        print(f"[summary_scope] MCP unavailable for scoped CR discovery; using direct Jira REST fallback: {exc}")
        candidate_jqls, custom_jql_configured = _cr_discovery_jql_candidates(project_key)
        if board_id:
            for jql in candidate_jqls:
                crs = await asyncio.to_thread(jira_rest.search_board_issues, board_id, jql, 500)
                if crs:
                    logger.info(
                        "[scope_cr_discovery] source=rest-board project=%s board=%s cr_count=%s jql=%r",
                        project_key or "-",
                        board_id or "-",
                        len(crs),
                        jql,
                    )
                    return crs, f'board = {board_id} AND ({jql})'
                if custom_jql_configured and jql == _scope_jql(CR_DISCOVERY_JQL, project_key):
                    logger.warning(
                        "[scope_cr_discovery_zero] source=rest-board project=%s board=%s cr_count=0 jql=%r",
                        project_key or "-",
                        board_id or "-",
                        jql,
                    )
                    return [], f'board = {board_id} AND ({jql})'
            return [], f'board = {board_id} AND ({candidate_jqls[0]})'

        for jql in candidate_jqls:
            crs = await _search_issues_with_fallback(jql, session=None)
            if crs:
                return crs, jql
            if custom_jql_configured and jql == _scope_jql(CR_DISCOVERY_JQL, project_key):
                return [], jql
        return [], candidate_jqls[0]


async def _discover_board_story_keys(board_id: str | None):
    """Return all Story issue keys currently visible in a board."""
    if not board_id:
        return None
    try:
        issues = await asyncio.to_thread(
            jira_rest.search_board_issues,
            board_id,
            None,
            1000,
            ["issuetype"],
        )
    except Exception as exc:
        print(f"[board_scope] story key discovery failed for board {board_id}: {exc}")
        logger.exception("[board_scope] story key discovery failed for board=%s", board_id)
        return set()
    story_keys = {i.get("key") for i in issues if i.get("key") and _is_story_issue(i)}
    logger.info(
        "[board_scope] board=%s board_issues=%s story_keys=%s",
        board_id,
        len(issues or []),
        len(story_keys),
    )
    if issues and not story_keys:
        logger.warning("[board_scope_zero] board=%s has issues but no story keys matched issue type filters", board_id)
    return story_keys


async def _discover_assignees_for_scope(project_key: str | None = None, board_id: str | None = None):
    """Collect unique assignees for CR/Story issues in the selected scope.

    Uses board-scoped direct queries (fast) and falls back to project-scoped
    search when board_id is not provided.
    """
    project_key = _effective_project_key(project_key, board_id)
    cache_key = (project_key or "", board_id or "")
    now = time.time()
    cached = _ASSIGNEE_SCOPE_CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0)) <= ASSIGNEE_SCOPE_CACHE_TTL_SEC:
        return cached["value"]

    assignees = {}
    used_jqls = []

    def _add_assignee(issue):
        name = _assignee_name(issue)
        if not name:
            return
        key = name.lower()
        if key not in assignees:
            assignees[key] = {"name": name}

    # Board scope: fetch board issues once and derive assignees directly.
    if board_id:
        try:
            board_issues = await asyncio.to_thread(
                jira_rest.search_board_issues,
                board_id,
                None,
                ASSIGNEE_DISCOVERY_MAX_ISSUES,
                ["assignee", "issuetype", "labels"],
            )
            for issue in board_issues:
                if _is_story_issue(issue) or _is_cr_issue(issue) or _has_cr_label(issue):
                    _add_assignee(issue)
            used_jqls.append(f"board:{board_id}:all-issues(limit={ASSIGNEE_DISCOVERY_MAX_ISSUES})")
        except Exception as exc:
            print(f"[jira_assignees] board query failed for board {board_id}: {exc}")
    else:
        # Project/global scope without board restriction.
        project_prefix = f'project = "{project_key}" AND ' if project_key else ""
        jqls = [
            f"{project_prefix}({CR_DISCOVERY_JQL})",
            f'{project_prefix}(issuetype = "Change Request")',
            f'{project_prefix}(issuetype = "CR")',
            f'{project_prefix}(issuetype = "Story")',
            f'{project_prefix}(issuetype = "User Story")',
        ]
        seen = set()
        for jql in jqls:
            if jql in seen:
                continue
            seen.add(jql)
            try:
                issues = await _search_issues_with_fallback(jql, session=None, max_results=1000)
                for issue in issues:
                    _add_assignee(issue)
                used_jqls.append(jql)
            except Exception as exc:
                print(f"[jira_assignees] project query failed for jql={jql!r}: {exc}")
                continue

    response = {
        "items": sorted(assignees.values(), key=lambda a: a["name"].lower()),
        "jql": "; ".join(used_jqls[:6]),
    }
    _ASSIGNEE_SCOPE_CACHE[cache_key] = {"ts": now, "value": response}
    return response


def _build_cr_dashboard_rows(crs, cr_field_findings):
    findings_by_key = {}
    for finding in cr_field_findings:
        findings_by_key.setdefault(finding.get("entity_key"), []).append(finding)

    rows = []
    for cr in crs:
        key = cr.get("key")
        if not key:
            continue
        findings = findings_by_key.get(key, [])
        max_severity = "OK"
        if findings:
            max_severity = max(
                (finding.get("severity") or "Low" for finding in findings),
                key=_severity_rank,
            )
        rows.append({
            "key": key,
            "summary": cr.get("summary") or "",
            "status": (cr.get("status") or {}).get("name") or "-",
            "assignee": _assignee_name(cr),
            "issue_count": len(findings),
            "max_severity": max_severity,
            "field_findings": findings,
        })

    rows.sort(key=lambda row: (-row["issue_count"], -_severity_rank(row["max_severity"]), row["key"]))
    return rows


# ---------------------------------------------------------------------------
# Core MCP call helper
# ---------------------------------------------------------------------------
async def _call_mcp_tool(tool_name: str, arguments: dict):
    """Opens a fresh SSE connection, calls one tool, closes the connection.
    Simple and reliable for a POC. For higher traffic, switch to a
    long-lived session managed in a background thread/event loop."""
    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            # result.content is a list of content blocks (usually text/json)
            return [block.model_dump() for block in result.content]


async def _list_mcp_tools():
    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [t.model_dump() for t in tools.tools]


def run_async(coro):
    """Run an async MCP call from a sync Flask route."""
    import traceback

    def _flatten(exc, depth=0):
        """Recursively unwrap ExceptionGroups (which can nest) to find the
        actual leaf errors, instead of stopping at the first group level."""
        if hasattr(exc, "exceptions") and depth < 5:
            leaves = []
            for sub in exc.exceptions:
                leaves.extend(_flatten(sub, depth + 1))
            return leaves
        return [f"{type(exc).__name__}: {exc}"]

    try:
        return asyncio.run(coro)
    except Exception as e:
        real_errors = _flatten(e)
        error_text = "; ".join(real_errors)
        if (
            "AuthenticationError" in error_text
            or "Unauthorized (401)" in error_text
            or "Authentication failed for Jira API (403)" in error_text
        ):
            return {
                "error": (
                    "Jira authentication failed. Check JIRA_URL, JIRA_USERNAME, and "
                    "JIRA_PASSWORD (or JIRA_API_TOKEN), then restart the containers."
                )
            }
        print("=== run_async error ===")
        traceback.print_exc()
        return {"error": error_text}


def _warn_if_mcp_unreachable():
    """Best-effort startup probe so connection failures are obvious in logs."""
    if not _is_mcp_port_reachable(timeout_seconds=5):
        print(f"[startup] Could not connect to MCP endpoint {MCP_SERVER_URL}")


def _mcp_host_port():
    parsed = urlparse(MCP_SERVER_URL)
    host = parsed.hostname or "localhost"
    if parsed.port:
        return host, parsed.port
    return host, 443 if parsed.scheme == "https" else 80


def _is_mcp_port_reachable(timeout_seconds=5):
    host, port = _mcp_host_port()
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _wait_for_mcp_ready(max_attempts=10, sleep_seconds=2):
    """Retry a few times so first scan doesn't fail during container warm-up."""
    for attempt in range(1, max_attempts + 1):
        if _is_mcp_port_reachable(timeout_seconds=5):
            return True
        print(f"[startup] MCP not ready yet ({attempt}/{max_attempts}); retrying in {sleep_seconds}s")
        time.sleep(sleep_seconds)
    print(f"[startup] MCP still unavailable after {max_attempts} attempts; skipping initial scan")
    return False


# ---------------------------------------------------------------------------
# Discovery: see exactly what tools + parameters your mcp-atlassian build exposes.
# Tool names/params can differ slightly between versions, so check this first.
# ---------------------------------------------------------------------------
@app.route("/api/tools", methods=["GET"])
def list_tools():
    result = run_async(_list_mcp_tools())
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/health/jira-auth", methods=["GET"])
def health_jira_auth():
    """Health check for dashboard Jira auth path (direct REST + optional MCP)."""
    jira_health = jira_rest.check_jira_auth()

    mcp_health = {
        "reachable": _is_mcp_port_reachable(timeout_seconds=3),
        "endpoint": MCP_SERVER_URL,
    }

    if mcp_health["reachable"]:
        mcp_tools = run_async(_list_mcp_tools())
        if isinstance(mcp_tools, dict) and "error" in mcp_tools:
            mcp_health["ok"] = False
            mcp_health["error"] = mcp_tools["error"]
        else:
            mcp_health["ok"] = True
            mcp_health["tool_count"] = len(mcp_tools)
    else:
        mcp_health["ok"] = False
        mcp_health["error"] = "MCP endpoint not reachable"

    body = {
        "jira": jira_health,
        "mcp": mcp_health,
    }
    code = 200 if jira_health.get("ok") else 503
    return jsonify(body), code


@app.route("/api/config", methods=["GET"])
def client_config():
    return jsonify({
        "jira_browse_base": _jira_browse_base(),
        "cr_discovery_jql": CR_DISCOVERY_JQL,
        "default_project_key": DEFAULT_PROJECT_KEY,
    })


@app.route("/api/jira/projects", methods=["GET"])
def jira_projects():
    try:
        projects = jira_rest.list_projects()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    projects.sort(key=lambda p: (p.get("key") != DEFAULT_PROJECT_KEY, p.get("name") or p.get("key") or ""))
    return jsonify({"items": projects, "default_project_key": DEFAULT_PROJECT_KEY})


@app.route("/api/jira/boards", methods=["GET"])
def jira_boards():
    project_key = (request.args.get("project_key") or "").strip().upper() or None
    try:
        boards = jira_rest.list_boards(project_key=project_key)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    boards.sort(key=lambda b: (b.get("name") or "").lower())
    return jsonify({"items": boards, "project_key": project_key})


@app.route("/api/jira/assignees", methods=["GET"])
def jira_assignees():
    project_key = (request.args.get("project_key") or "").strip().upper() or None
    board_id = (request.args.get("board_id") or "").strip() or None
    project_key = _effective_project_key(project_key, board_id)

    result = run_async(_discover_assignees_for_scope(project_key=project_key, board_id=board_id))
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# Generic passthrough: call any tool by name with any arguments.
# Useful once you've confirmed exact names/params via /api/tools.
@app.route("/api/call/<tool_name>", methods=["POST"])
def call_tool_generic(tool_name):
    arguments = request.get_json(silent=True) or {}
    result = run_async(_call_mcp_tool(tool_name, arguments))
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# ---------------------------------------------------------------------------
# Convenience endpoints for the most common read/write operations.
# Tool names below match sooperset/mcp-atlassian as of mid-2026 — confirm
# against /api/tools if your build differs.
# ---------------------------------------------------------------------------

# READ: get a single issue
@app.route("/api/2/issues/<issue_key>", methods=["GET"])
def get_issue(issue_key):
    result = run_async(_call_mcp_tool("jira_get_issue", {"issue_key": issue_key}))
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# READ: search issues via JQL
@app.route("/api/search", methods=["GET"])
def search_issues():
    jql = request.args.get("jql", "")
    if not jql:
        return jsonify({"error": "Provide a jql query param, e.g. ?jql=project=PROJ"}), 400
    result = run_async(_call_mcp_tool("jira_search", {"jql": jql}))
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# WRITE: create an issue
@app.route("/api/issues", methods=["POST"])
def create_issue():
    body = request.get_json(silent=True) or {}
    required = ["project_key", "summary", "issue_type"]
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify({"error": f"Missing required fields: {missing}"}), 400

    arguments = {
        "project_key": body["project_key"],
        "summary": body["summary"],
        "issue_type": body["issue_type"],
        "description": body.get("description", ""),
    }
    result = run_async(_call_mcp_tool("jira_create_issue", arguments))
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# WRITE: add a comment to an issue
@app.route("/api/issues/<issue_key>/comment", methods=["POST"])
def add_comment(issue_key):
    body = request.get_json(silent=True) or {}
    if "comment" not in body:
        return jsonify({"error": "Provide 'comment' in the request body"}), 400
    result = run_async(
        _call_mcp_tool("jira_add_comment", {"issue_key": issue_key, "comment": body["comment"]})
    )
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# WRITE: transition an issue's status (e.g. To Do -> In Progress)
@app.route("/api/2/issues/<issue_key>/transition", methods=["POST"])
def transition_issue(issue_key):
    body = request.get_json(silent=True) or {}
    if "transition" not in body:
        return jsonify({"error": "Provide 'transition' (status name or id) in the request body"}), 400
    result = run_async(
        _call_mcp_tool(
            "jira_transition_issue", {"issue_key": issue_key, "transition": body["transition"]}
        )
    )
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# ---------------------------------------------------------------------------
# Discovery: show exact schemas for the tools this scenario depends on, so
# you can confirm field names before running real writes against Jira.
# ---------------------------------------------------------------------------
@app.route("/api/scenario/discover", methods=["GET"])
def discover_scenario_tools():
    tools = run_async(_list_mcp_tools())
    if isinstance(tools, dict) and "error" in tools:
        return jsonify(tools), 500
    keywords = ["create_issue", "link", "epic", "search", "get_issue"]
    relevant = [t for t in tools if any(k in t.get("name", "") for k in keywords)]
    return jsonify(relevant)


def _find_tool_name(all_tools, must_contain):
    """Return the first tool name containing all given substrings."""
    for t in all_tools:
        name = t.get("name", "")
        if all(s in name for s in must_contain):
            return name
    return None


# ---------------------------------------------------------------------------
# Scenario: create an Epic, N Stories under it (team-managed projects use
# the 'parent' field for epic linkage), a CR issue per story, and an issue
# link between each Story and its CR.
#
# POST body:
# {
#   "project_key": "SCRUM",
#   "epic_summary": "Q3 Platform Revamp",
#   "cr_issue_type": "Change Request",
#   "link_type": "relates to",
#   "stories": [
#     {"summary": "Story A", "description": "..."},
#     {"summary": "Story B", "description": "..."}
#   ]
# }
# ---------------------------------------------------------------------------
@app.route("/api/scenario/create", methods=["POST"])
def create_scenario():
    body = request.get_json(silent=True) or {}
    project_key = body.get("project_key")
    epic_summary = body.get("epic_summary")
    stories = body.get("stories", [])
    cr_issue_type = body.get("cr_issue_type", "Change Request")
    link_type = body.get("link_type", "relates to")

    if not project_key or not epic_summary or not stories:
        return jsonify({"error": "project_key, epic_summary, and at least one story are required"}), 400

    all_tools = run_async(_list_mcp_tools())
    if isinstance(all_tools, dict) and "error" in all_tools:
        return jsonify({"error": "Could not reach MCP server", "detail": all_tools}), 500

    create_tool = _find_tool_name(all_tools, ["create_issue"]) or "jira_create_issue"
    # exclude create_issue_link from matching create_issue search above
    if "link" in create_tool:
        create_tool = "jira_create_issue"
    link_tool = _find_tool_name(all_tools, ["create", "link"]) or _find_tool_name(all_tools, ["link"])

    result = {"epic": None, "stories": [], "errors": []}

    async def run_scenario():
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 1. Create Epic
                epic_res = await session.call_tool(create_tool, {
                    "project_key": project_key,
                    "summary": epic_summary,
                    "issue_type": "Epic",
                })
                epic_blocks = [b.model_dump() for b in epic_res.content]
                result["epic"] = epic_blocks
                epic_key = _extract_key(epic_blocks)
                if not epic_key:
                    result["errors"].append("Could not extract Epic key from create response — check 'epic' block above")
                    return

                # 2. Create each Story, linked to the Epic via 'parent'
                for story in stories:
                    story_entry = {"summary": story.get("summary"), "story": None, "cr": None, "link": None}
                    story_res = await session.call_tool(create_tool, {
                        "project_key": project_key,
                        "summary": story.get("summary"),
                        "issue_type": "Story",
                        "description": story.get("description", ""),
                        "additional_fields": {"parent": {"key": epic_key}},
                    })
                    story_blocks = [b.model_dump() for b in story_res.content]
                    story_entry["story"] = story_blocks
                    story_key = _extract_key(story_blocks)

                    if story_key:
                        # 3. Create a CR issue for this story
                        cr_res = await session.call_tool(create_tool, {
                            "project_key": project_key,
                            "summary": f"CR for {story_key}: {story.get('summary')}",
                            "issue_type": cr_issue_type,
                            "description": f"Change request associated with {story_key}",
                        })
                        cr_blocks = [b.model_dump() for b in cr_res.content]
                        story_entry["cr"] = cr_blocks
                        cr_key = _extract_key(cr_blocks)

                        # 4. Link Story <-> CR
                        if cr_key and link_tool:
                            try:
                                link_res = await session.call_tool(link_tool, {
                                    "link_type": link_type,
                                    "inward_issue_key": story_key,
                                    "outward_issue_key": cr_key,
                                })
                                story_entry["link"] = [b.model_dump() for b in link_res.content]
                            except Exception as e:
                                story_entry["link"] = {"error": str(e)}
                        elif not link_tool:
                            story_entry["link"] = {"error": "No link tool found — check /api/scenario/discover"}

                    result["stories"].append(story_entry)

    run_result = run_async(run_scenario())
    if isinstance(run_result, dict) and "error" in run_result:
        return jsonify(run_result), 500
    return jsonify(result)


def _extract_key(content_blocks):
    """Best-effort pull of an issue 'key' (e.g. SCRUM-12) out of a tool result."""
    import json as _json
    for block in content_blocks:
        text = block.get("text")
        if not text:
            continue
        try:
            parsed = _json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            if "key" in parsed:
                return parsed["key"]
            if "issue" in parsed and isinstance(parsed["issue"], dict) and "key" in parsed["issue"]:
                return parsed["issue"]["key"]
    return None


# ---------------------------------------------------------------------------
# Find Stories under an Epic whose status differs from their linked CR's status
# ---------------------------------------------------------------------------
@app.route("/api/scenario/mismatches", methods=["GET"])
def scenario_mismatches():
    epic_key = request.args.get("epic_key")
    if not epic_key:
        return jsonify({"error": "Provide ?epic_key=EPIC-1"}), 400

    async def run_check():
        import json as _json
        mismatches = []
        checked = []
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                search_res = await session.call_tool(
                    "jira_search", {"jql": f'parent = {epic_key} AND type = Story'}
                )
                search_blocks = [b.model_dump() for b in search_res.content]
                stories = []
                for block in search_blocks:
                    text = block.get("text")
                    if not text:
                        continue
                    try:
                        parsed = _json.loads(text)
                        stories = parsed.get("issues", [])
                    except Exception:
                        pass

                for story in stories:
                    story_key = story.get("key")
                    story_status = (story.get("status") or {}).get("name")

                    issue_res = await session.call_tool(
                        "jira_get_issue", {"issue_key": story_key, "include": "links"}
                    )
                    issue_blocks = [b.model_dump() for b in issue_res.content]
                    cr_key, cr_status = None, None
                    for block in issue_blocks:
                        text = block.get("text")
                        if not text:
                            continue
                        try:
                            parsed = _json.loads(text)
                        except Exception:
                            continue
                        links = parsed.get("links") or parsed.get("issuelinks") or []
                        for link in links:
                            linked = link.get("issue") or link.get("outward_issue") or link.get("inward_issue")
                            if linked and "CR" in str(linked.get("summary", "")) or (
                                linked and linked.get("issue_type", {}).get("name") == "Change Request"
                            ):
                                cr_key = linked.get("key")
                                cr_status = (linked.get("status") or {}).get("name")

                    entry = {
                        "story_key": story_key,
                        "story_status": story_status,
                        "cr_key": cr_key,
                        "cr_status": cr_status,
                    }
                    checked.append(entry)
                    if cr_key:
                        compliant, reason = is_compliant(cr_status, story_status)
                        entry["compliance_reason"] = reason
                        if not compliant:
                            mismatches.append(entry)

        return {"checked": checked, "mismatches": mismatches}

    result = run_async(run_check())
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# ---------------------------------------------------------------------------
# One CR, many Stories linked to it — find Stories whose status differs
# from that CR's status.
# ---------------------------------------------------------------------------
@app.route("/api/scenario/cr-mismatches", methods=["GET"])
def cr_mismatches():
    cr_key = request.args.get("cr_key")
    if not cr_key:
        return jsonify({"error": "Provide ?cr_key=CR-1"}), 400

    async def run_check():
        import json as _json

        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 1. Get the CR's own status
                cr_res = await session.call_tool("jira_get_issue", {"issue_key": cr_key})
                cr_parsed = None
                for block in [b.model_dump() for b in cr_res.content]:
                    text = block.get("text")
                    if not text:
                        continue
                    try:
                        cr_parsed = _json.loads(text)
                        break
                    except Exception:
                        continue
                if not cr_parsed:
                    return {"error": "Could not parse CR issue"}
                cr_status = (cr_parsed.get("status") or {}).get("name")

                # 2. Find every issue linked to the CR via JQL, instead of relying
                #    on jira_get_issue's links field (unreliable across versions).
                search_res = await session.call_tool(
                    "jira_search", {"jql": f'issue in linkedIssues("{cr_key}")'}
                )
                linked_issues = []
                for block in [b.model_dump() for b in search_res.content]:
                    text = block.get("text")
                    if not text:
                        continue
                    try:
                        parsed = _json.loads(text)
                        linked_issues = parsed.get("issues", [])
                    except Exception:
                        pass

                checked, mismatches = [], []
                for issue in linked_issues:
                    issue_type = (issue.get("issue_type") or {}).get("name")
                    if issue_type != "Story":
                        continue  # skip non-Story linked issues
                    story_key = issue.get("key")
                    story_status = (issue.get("status") or {}).get("name")
                    entry = {
                        "story_key": story_key,
                        "story_status": story_status,
                        "cr_key": cr_key,
                        "cr_status": cr_status,
                    }
                    compliant, reason = is_compliant(cr_status, story_status)
                    entry["compliance_reason"] = reason
                    checked.append(entry)
                    if not compliant:
                        mismatches.append(entry)

                return {"cr_key": cr_key, "cr_status": cr_status, "checked": checked, "mismatches": mismatches}

    result = run_async(run_check())
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# ---------------------------------------------------------------------------
# Full compliance scan: discover every CR (issue_type = Task, label = CR — or
# a real "Change Request" issue type if you have one), find every Story
# linked to it, evaluate compliance, and persist the whole run to SQLite.
# This is what the scheduler calls on a timer, and what /refresh calls
# on demand.
# ---------------------------------------------------------------------------
OUTCOME_DISCOVERY_JQL = os.environ.get("OUTCOME_DISCOVERY_JQL", 'issuetype = "Outcome"')
EPIC_DISCOVERY_JQL = os.environ.get("EPIC_DISCOVERY_JQL", 'issuetype = "Epic"')


def _ensure_classified(status_type, raw_status):
    """If this status isn't already understood (natively or via a learned
    alias), ask the local LLM to classify it, learn the result in-memory,
    and persist it so future scans/restarts don't re-ask. Silently leaves
    the status unclassified (falls through to 'Unmapped status' in
    compliance.evaluate) if Ollama is unreachable — never blocks the scan."""
    if not raw_status or compliance_rules.is_known(status_type, raw_status):
        return
    known_fn = {
        "cr": compliance_rules.known_cr_statuses,
        "story": compliance_rules.known_story_statuses,
        "epic": compliance_rules.known_epic_statuses,
        "outcome": compliance_rules.known_outcome_statuses,
    }.get(status_type, compliance_rules.known_story_statuses)
    known = known_fn()
    try:
        result = ai_classify.classify_status(raw_status, status_type, known)
    except Exception as e:
        print(f"[ai_classify] could not classify {status_type} status {raw_status!r}: {e}")
        return
    compliance_rules.learn_alias(status_type, raw_status, result["target"])
    compliance_db.save_status_mapping(
        status_type, raw_status, result["target"], "ai", result.get("reasoning", ""), result.get("confidence", "")
    )
    print(f"[ai_classify] learned: {status_type} '{raw_status}' -> '{result['target']}' ({result.get('confidence')})")


async def _run_full_scan(project_key: str | None = None, board_id: str | None = None):
    import json as _json
    import uuid as _uuid
    import datetime

    field_run_id = str(_uuid.uuid4())
    results = []
    field_findings_by_entity = []  # [(entity_type, entity_key, entity_status, findings)]
    epic_to_cr_entries = {}  # epic_key -> [(cr_key, cr_status), ...]
    epic_results = []
    outcome_results = []
    board_story_keys = await _discover_board_story_keys(board_id)
    board_scoped_mode = bool(board_id)

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            crs, cr_discovery_jql = await _discover_crs(session, project_key=project_key, board_id=board_id)
            if board_scoped_mode and SCOPED_SCAN_MAX_CRS > 0 and len(crs) > SCOPED_SCAN_MAX_CRS:
                print(
                    f"[scan] board-scoped CR list too large ({len(crs)}). "
                    f"Limiting to first {SCOPED_SCAN_MAX_CRS} for refresh responsiveness."
                )
                crs = crs[:SCOPED_SCAN_MAX_CRS]
            print(f"[cr_discovery] scan returned {len(crs)} CRs using JQL: {cr_discovery_jql}")

            for cr in crs:
                cr_key = cr.get("key")
                cr_status = (cr.get("status") or {}).get("name")
                cr_assignee = _assignee_name(cr)
                _ensure_classified("cr", cr_status)

                # Field-completeness check for this CR — direct REST call,
                # since custom fields don't come through the MCP tool's
                # curated response. Also reused to find the CR's Epic Link
                # for the Epic<-CR bottleneck check below, avoiding a
                # second fetch per CR.
                try:
                    raw = jira_rest.get_issue_raw(cr_key)
                    raw_fields = raw.get("fields", {})
                    cr_findings = field_rules.check_cr_fields(raw_fields, cr_status)
                    field_findings_by_entity.append(("cr", cr_key, cr_status, cr_findings))

                    epic_key = None
                    parent = raw_fields.get("parent")
                    if parent and isinstance(parent, dict):
                        epic_key = parent.get("key")
                    if not epic_key:
                        epic_key = raw_fields.get(field_rules.CR_FIELDS["epic_link"])
                    if epic_key:
                        epic_to_cr_entries.setdefault(epic_key, []).append((cr_key, cr_status))
                except Exception as e:
                    print(f"[field_rules] could not check CR {cr_key}: {e}")

                linked = await _search_issues_with_fallback(
                    f'issue in linkedIssues("{cr_key}")',
                    session=session,
                )

                for issue in linked:
                    if not _is_story_issue(issue):
                        continue
                    story_key = issue.get("key")
                    if board_story_keys is not None and story_key not in board_story_keys:
                        continue
                    story_status = (issue.get("status") or {}).get("name")
                    _ensure_classified("story", story_status)

                    # age_days: how long since the story's last update, as a
                    # rough proxy for how long it's been sitting non-compliant.
                    # Falls back to 0 if 'updated' isn't in the expected format.
                    age_days = 0
                    updated = issue.get("updated")
                    if updated:
                        try:
                            updated_dt = datetime.datetime.strptime(updated[:19], "%Y-%m-%d %H:%M:%S")
                            age_days = (datetime.datetime.utcnow() - updated_dt).days
                        except Exception:
                            pass

                    verdict = compliance_rules.evaluate(cr_status, story_status, age_days)
                    results.append({
                        "pair_type": "cr_story",
                        "cr_key": cr_key, "cr_status": cr_status,
                        "cr_assignee": cr_assignee,
                        "story_key": story_key, "story_status": story_status,
                        "story_assignee": _assignee_name(issue),
                        "compliant": verdict["compliant"], "reason": verdict["reason"],
                        "severity": verdict["severity"], "score": verdict["score"],
                    })

            if not board_scoped_mode:
                # Epic <- CR bottleneck check, using the Epic Links collected above.
                epics = await _search_issues_with_fallback(_scope_jql(EPIC_DISCOVERY_JQL, project_key), session=session)

                for epic in epics:
                    epic_key = epic.get("key")
                    epic_status = (epic.get("status") or {}).get("name")
                    _ensure_classified("epic", epic_status)
                    cr_entries = epic_to_cr_entries.get(epic_key, [])
                    verdict = compliance_rules.evaluate_epic(epic_status, cr_entries)
                    if verdict["bottleneck_cr_key"] is None:
                        continue  # no linked CRs — nothing to compare, skip rather than record a hollow row
                    epic_results.append({
                        "pair_type": "epic_cr",
                        "cr_key": verdict["bottleneck_cr_key"], "cr_status": verdict["bottleneck_cr_status"],
                        "cr_assignee": None,
                        "story_key": epic_key, "story_status": epic_status,
                        "story_assignee": None,
                        "compliant": verdict["compliant"], "reason": verdict["reason"],
                        "severity": verdict["severity"], "score": verdict["score"],
                    })

                # Outcome field-completeness + Story <- Outcome phase alignment
                outcomes = await _search_issues_with_fallback(_scope_jql(OUTCOME_DISCOVERY_JQL, project_key), session=session)

                for outcome in outcomes:
                    outcome_key = outcome.get("key")
                    outcome_status = (outcome.get("status") or {}).get("name")
                    _ensure_classified("outcome", outcome_status)
                    try:
                        raw = jira_rest.get_issue_raw(outcome_key)
                        outcome_findings = field_rules.check_outcome_fields(raw.get("fields", {}), outcome_status)
                        field_findings_by_entity.append(("outcome", outcome_key, outcome_status, outcome_findings))
                    except Exception as e:
                        print(f"[field_rules] could not check Outcome {outcome_key}: {e}")

                    outcome_linked = await _search_issues_with_fallback(
                        f'issue in linkedIssues("{outcome_key}")',
                        session=session,
                    )

                    for issue in outcome_linked:
                        if not _is_story_issue(issue):
                            continue
                        story_key = issue.get("key")
                        story_status = (issue.get("status") or {}).get("name")
                        verdict = compliance_rules.evaluate_outcome(story_status, outcome_status)
                        outcome_results.append({
                            "pair_type": "story_outcome",
                            "cr_key": story_key, "cr_status": story_status,
                            "cr_assignee": None,
                            "story_key": outcome_key, "story_status": outcome_status,
                            "story_assignee": None,
                            "compliant": verdict["compliant"], "reason": verdict["reason"],
                            "severity": verdict["severity"], "score": verdict["score"],
                        })
            else:
                print("[scan] board-scoped refresh: skipped Epic/Outcome scans to reduce latency")

    run_id = compliance_db.save_run(results)
    if epic_results:
        compliance_db.save_additional_checks(run_id, epic_results, "epic_cr")
    if outcome_results:
        compliance_db.save_additional_checks(run_id, outcome_results, "story_outcome")
    for entity_type, entity_key, entity_status, findings in field_findings_by_entity:
        compliance_db.save_field_findings(field_run_id, entity_type, entity_key, entity_status, findings)

    return {
        "crs_scanned": len(set(r["cr_key"] for r in results)),
        "stories_scanned": len(results),
        "epics_scanned": len(epic_results),
        "outcomes_scanned": len(outcome_results),
        "field_entities_scanned": len(field_findings_by_entity),
        "field_findings_total": sum(len(f) for _, _, _, f in field_findings_by_entity),
    }


@app.route("/api/crs", methods=["GET"])
def get_all_crs():
    project_key = (request.args.get("project_key") or "").strip().upper() or None
    board_id = (request.args.get("board_id") or "").strip() or None
    project_key = _effective_project_key(project_key, board_id)
    non_compliant_only = (request.args.get("non_compliant_only") or "").strip().lower() in {"1", "true", "yes"}

    async def _load_crs():
        try:
            async with sse_client(MCP_SERVER_URL) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await _discover_crs(session, project_key=project_key, board_id=board_id)
        except Exception as exc:
            print(f"[api/crs] MCP unavailable for CR discovery; using direct Jira REST fallback: {exc}")
            candidate_jqls, custom_jql_configured = _cr_discovery_jql_candidates(project_key)
            if board_id:
                for jql in candidate_jqls:
                    crs = await asyncio.to_thread(jira_rest.search_board_issues, board_id, jql, 500)
                    if crs:
                        return crs, f'board = {board_id} AND ({jql})'
                    if custom_jql_configured and jql == _scope_jql(CR_DISCOVERY_JQL, project_key):
                        return [], f'board = {board_id} AND ({jql})'
                return [], f'board = {board_id} AND ({candidate_jqls[0]})'

            for jql in candidate_jqls:
                crs = await _search_issues_with_fallback(jql, session=None)
                if crs:
                    return crs, jql
                if custom_jql_configured and jql == _scope_jql(CR_DISCOVERY_JQL, project_key):
                    return [], jql
            return [], candidate_jqls[0]

    result = run_async(_load_crs())
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    crs, used_jql = result

    if non_compliant_only:
        summary = compliance_db.get_latest_summary()
        board_story_keys = None
        if board_id:
            board_story_result = run_async(_discover_board_story_keys(board_id))
            if isinstance(board_story_result, dict) and "error" in board_story_result:
                return jsonify(board_story_result), 500
            board_story_keys = board_story_result if isinstance(board_story_result, set) else set(board_story_result or [])
        scoped_cr_keys = {cr.get("key") for cr in crs if cr.get("key")}
        filtered_active = _filter_active_rows(
            summary.get("active", []),
            scoped_cr_keys,
            board_story_keys=board_story_keys,
        )
        active_cr_keys = {row.get("cr_key") for row in filtered_active if row.get("cr_key")}
        crs = [cr for cr in crs if cr.get("key") in active_cr_keys]

    cr_findings = compliance_db.get_latest_field_findings("cr")
    return jsonify({
        "items": _build_cr_dashboard_rows(crs, cr_findings),
        "jql": used_jql,
    })


@app.route("/api/pairs/epic-cr", methods=["GET"])
def get_epic_cr_findings():
    run_id = compliance_db.latest_run_id()
    if not run_id:
        return jsonify([])
    return jsonify(compliance_db.get_pair_findings(run_id, "epic_cr"))


@app.route("/api/pairs/story-outcome", methods=["GET"])
def get_story_outcome_findings():
    run_id = compliance_db.latest_run_id()
    if not run_id:
        return jsonify([])
    return jsonify(compliance_db.get_pair_findings(run_id, "story_outcome"))


@app.route("/api/fields/findings", methods=["GET"])
def get_field_findings():
    entity_type = request.args.get("entity_type")  # 'cr' or 'outcome', optional
    return jsonify(compliance_db.get_latest_field_findings(entity_type))


@app.route("/api/fields/check", methods=["POST"])
def check_single_issue_fields():
    """Ad-hoc check of one issue, without waiting for the next full scan.
    Body: {"issue_key": "TPOC-12", "entity_type": "cr"} (or "outcome")"""
    body = request.get_json(silent=True) or {}
    issue_key, entity_type = body.get("issue_key"), body.get("entity_type")
    if not issue_key or entity_type not in ("cr", "outcome"):
        return jsonify({"error": "Provide issue_key and entity_type ('cr' or 'outcome')"}), 400
    try:
        raw = jira_rest.get_issue_raw(issue_key)
    except Exception as e:
        return jsonify({"error": f"Could not fetch issue: {e}"}), 500

    status_name = ((raw.get("fields", {}).get("status") or {}).get("name"))
    if entity_type == "cr":
        findings = field_rules.check_cr_fields(raw.get("fields", {}), status_name)
    else:
        findings = field_rules.check_outcome_fields(raw.get("fields", {}), status_name)

    return jsonify({"issue_key": issue_key, "status": status_name, "findings": findings})


@app.route("/api/mappings", methods=["GET"])
def get_mappings():
    return jsonify(compliance_db.get_all_status_mappings())


@app.route("/api/mappings/pending", methods=["GET"])
def get_pending_mappings():
    return jsonify(compliance_db.get_unreviewed_status_mappings())


@app.route("/api/mappings/override", methods=["POST"])
def override_mapping():
    """Human confirms or corrects an AI-learned mapping. Overwrites the
    stored target and marks it reviewed so it won't show up as pending
    again, and takes effect immediately for subsequent scans."""
    body = request.get_json(silent=True) or {}
    status_type, raw_status, target_status = body.get("status_type"), body.get("raw_status"), body.get("target_status")
    if not all([status_type, raw_status, target_status]):
        return jsonify({"error": "Provide status_type, raw_status, target_status"}), 400
    compliance_rules.learn_alias(status_type, raw_status, target_status)
    compliance_db.save_status_mapping(status_type, raw_status, target_status, "human", "manual override", "confirmed")
    return jsonify({"updated": True})


@app.route("/api/dashboard/refresh", methods=["POST"])
def dashboard_refresh():
    body = request.get_json(silent=True) or {}
    project_key = (body.get("project_key") or "").strip().upper() or None
    board_id = str(body.get("board_id") or "").strip() or None
    project_key = _effective_project_key(project_key, board_id)
    logger.info(
            "[_effective_project_key] project=%s board=%s ",
            project_key or "-",
            board_id or "-",
        )
    result = run_async(_run_full_scan(project_key=project_key, board_id=board_id))

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    logger.info(
                "[_effective_project_key] project=%s result=%s ",
                project_key or "-",
                result or "-",
            )
    return jsonify(result)


@app.route("/api/dashboard/summary", methods=["GET"])
def dashboard_summary():
    project_key = (request.args.get("project_key") or "").strip().upper() or None
    board_id = (request.args.get("board_id") or "").strip() or None
    project_key = _effective_project_key(project_key, board_id)
    debug_enabled = (request.args.get("debug") or "").strip().lower() in {"1", "true", "yes"}
    summary = compliance_db.get_latest_summary()
    logger.info(
        "[summary_scope_start] project=%s board=%s base_non_compliant=%s",
        project_key or "-",
        board_id or "-",
        len(summary.get("active", [])),
    )
    logger.info(
        "[summary_scope_start] project=%s board=%s Detailed summary=%s",
        project_key or "-",
        board_id or "-",
        summary,
    )

    # If no scope is requested, preserve existing behavior.
    if not project_key and not board_id:
        return jsonify(summary)

    scoped_cr_result = run_async(_discover_crs_for_scope(project_key=project_key, board_id=board_id))
    if isinstance(scoped_cr_result, dict) and "error" in scoped_cr_result:
        return jsonify(scoped_cr_result), 500

    scoped_crs, used_jql = scoped_cr_result
    scoped_cr_keys = {cr.get("key") for cr in scoped_crs if cr.get("key")}
    board_story_keys = None

    if board_id:
        board_story_result = run_async(_discover_board_story_keys(board_id))
        if isinstance(board_story_result, dict) and "error" in board_story_result:
            return jsonify(board_story_result), 500
        board_story_keys = board_story_result if isinstance(board_story_result, set) else set(board_story_result or [])
        logger.info(
            "[summary_scope_board] board=%s board_story_keys=%s",
            board_id,
            len(board_story_keys),
        )

    scoped_active = _filter_active_rows(
        summary.get("active", []),
        scoped_cr_keys,
        board_story_keys=board_story_keys,
    )

    scoped_summary = dict(summary)
    scoped_summary["active"] = scoped_active
    scoped_summary["non_compliant"] = len(scoped_active)
    non_compliant_cr_keys = sorted({row.get("cr_key") for row in scoped_active if row.get("cr_key")})
    scoped_summary["scope"] = {
        "project_key": project_key,
        "board_id": board_id,
        "cr_discovery_jql": used_jql,
        "crs_in_scope": len(scoped_cr_keys),
        "non_compliant_crs": len(non_compliant_cr_keys),
    }
    logger.info(
        "[summary_scope_result] project=%s board=%s crs_in_scope=%s non_compliant_pairs=%s non_compliant_crs=%s",
        project_key or "-",
        board_id or "-",
        len(scoped_cr_keys),
        len(scoped_active),
        len(non_compliant_cr_keys),
    )

    if board_id and (len(scoped_cr_keys) == 0 or len(scoped_active) == 0):
        logger.warning(
            "[summary_scope_zero] board=%s likely caused zero results. crs_in_scope=%s board_story_keys=%s non_compliant_pairs=%s jql=%r",
            board_id,
            len(scoped_cr_keys),
            len(board_story_keys) if board_story_keys is not None else "-",
            len(scoped_active),
            used_jql,
        )

    # Scoped debugging for board/project selection issues.
    print(
        "[summary_scope] "
        f"project={project_key or '-'} board={board_id or '-'} "
        f"jql={used_jql!r} crs_in_scope={len(scoped_cr_keys)} "
        f"non_compliant_pairs={len(scoped_active)} non_compliant_crs={len(non_compliant_cr_keys)}"
    )
    if non_compliant_cr_keys:
        print(f"[summary_scope] non_compliant_cr_keys={', '.join(non_compliant_cr_keys)}")

    if debug_enabled:
        scoped_summary["scope_debug"] = {
            "non_compliant_cr_keys": non_compliant_cr_keys,
            "non_compliant_pairs_preview": [
                {
                    "cr_key": row.get("cr_key"),
                    "cr_status": row.get("cr_status"),
                    "story_key": row.get("story_key"),
                    "story_status": row.get("story_status"),
                    "cr_assignee": row.get("cr_assignee"),
                    "story_assignee": row.get("story_assignee"),
                    "reason": row.get("reason"),
                    "severity": row.get("severity"),
                }
                for row in scoped_active[:50]
            ],
        }
    return jsonify(scoped_summary)


@app.route("/api/dashboard/trend", methods=["GET"])
def dashboard_trend():
    days = int(request.args.get("days", 14))
    return jsonify(compliance_db.get_trend(days))


@app.route("/api/dashboard/breakdown", methods=["GET"])
def dashboard_breakdown():
    summary = compliance_db.get_latest_summary()
    active = summary["active"]
    reason_counts, severity_counts, cr_counts = {}, {}, {}
    for r in active:
        reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1
        severity_counts[r["severity"]] = severity_counts.get(r["severity"], 0) + 1
        cr_counts[r["cr_key"]] = cr_counts.get(r["cr_key"], 0) + 1
    return jsonify({"by_reason": reason_counts, "by_severity": severity_counts, "by_cr": cr_counts})


@app.route("/api/dashboard/resolve", methods=["POST"])
def dashboard_resolve():
    body = request.get_json(silent=True) or {}
    story_key, cr_key = body.get("story_key"), body.get("cr_key")
    if not story_key or not cr_key:
        return jsonify({"error": "Provide story_key and cr_key"}), 400
    compliance_db.resolve(story_key, cr_key)
    return jsonify({"resolved": True, "story_key": story_key, "cr_key": cr_key})


@app.route("/api/dashboard/unresolve", methods=["POST"])
def dashboard_unresolve():
    body = request.get_json(silent=True) or {}
    story_key, cr_key = body.get("story_key"), body.get("cr_key")
    if not story_key or not cr_key:
        return jsonify({"error": "Provide story_key and cr_key"}), 400
    compliance_db.unresolve(story_key, cr_key)
    return jsonify({"resolved": False, "story_key": story_key, "cr_key": cr_key})


# ---------------------------------------------------------------------------
# Scheduler: re-run the full scan on an interval so the dashboard reflects
# near-real-time state without the user manually clicking refresh.
# ---------------------------------------------------------------------------
def start_scheduler():
    import threading
    from apscheduler.schedulers.background import BackgroundScheduler
    interval_minutes = int(os.environ.get("SCAN_INTERVAL_MINUTES", 15))
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: run_async(_run_full_scan()), "interval", minutes=interval_minutes)
    scheduler.start()

    # Run first scan in background so Flask starts serving /dashboard immediately.
    def _initial_scan_worker():
        if _wait_for_mcp_ready(
            max_attempts=int(os.environ.get("MCP_READY_RETRIES", 10)),
            sleep_seconds=int(os.environ.get("MCP_READY_RETRY_SECONDS", 2)),
        ):
            run_async(_run_full_scan())

    threading.Thread(target=_initial_scan_worker, name="initial-scan", daemon=True).start()
    return scheduler


# ---------------------------------------------------------------------------
# AI layer: draft a remediation comment via a local LLM (Ollama). This never
# writes to Jira on its own — it only returns text for a human to review.
# The separate /post endpoint below is the only thing that actually writes,
# and only fires when explicitly called after approval.
# ---------------------------------------------------------------------------
@app.route("/api/ai/draft-comment", methods=["POST"])
def ai_draft_comment():
    body = request.get_json(silent=True) or {}
    story_key, cr_key = body.get("story_key"), body.get("cr_key")
    if not story_key or not cr_key:
        return jsonify({"error": "Provide story_key and cr_key"}), 400

    # Pull the latest known mismatch details for this pair from the DB
    summary = compliance_db.get_latest_summary()
    match = next((r for r in summary["active"]
                  if r["story_key"] == story_key and r["cr_key"] == cr_key), None)
    if not match:
        return jsonify({"error": "No active mismatch found for that story/CR pair. Run a scan first."}), 404

    # Optionally enrich with the story's description for a more grounded draft
    story_desc = ""
    desc_result = run_async(_call_mcp_tool("jira_get_issue", {"issue_key": story_key}))
    if isinstance(desc_result, list):
        import json as _json
        for block in desc_result:
            text = block.get("text")
            if not text:
                continue
            try:
                parsed = _json.loads(text)
                story_desc = parsed.get("description", "")
                break
            except Exception:
                continue

    try:
        draft = ai_draft.draft_remediation_comment(
            story_key=story_key, story_status=match["story_status"], story_desc=story_desc,
            cr_key=cr_key, cr_status=match["cr_status"], reason=match["reason"], severity=match["severity"],
        )
    except Exception as e:
        return jsonify({"error": f"Local LLM call failed: {e}. Is Ollama running (ollama serve)?"}), 500

    return jsonify({"story_key": story_key, "cr_key": cr_key, "draft": draft})


@app.route("/api/ai/post-comment", methods=["POST"])
def ai_post_comment():
    """Only call this after a human has reviewed/edited the draft."""
    body = request.get_json(silent=True) or {}
    story_key, comment = body.get("story_key"), body.get("comment")
    if not story_key or not comment:
        return jsonify({"error": "Provide story_key and comment"}), 400
    result = run_async(_call_mcp_tool("jira_add_comment", {"issue_key": story_key, "comment": comment}))
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify({"posted": True, "story_key": story_key, "result": result})


# ---------------------------------------------------------------------------
# Serve the simple test UI
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/dashboard")
def dashboard():
    return send_from_directory(app.static_folder, "dashboard.html")


if __name__ == "__main__":
    # avoid starting the scheduler twice under Flask's debug reloader
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and not app.debug:
        start_scheduler()
    elif os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=True)
