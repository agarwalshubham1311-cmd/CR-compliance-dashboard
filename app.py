import asyncio
import atexit
import copy
import json
import os
import re
import sys
import threading
import time
import requests
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed


from flask import Flask, jsonify, request, send_from_directory, session, g, has_request_context
from mcp import ClientSession
from mcp.client.sse import sse_client

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")


@app.before_request
def _log_api_request_started():
    if not request.path.startswith("/api/"):
        return
    g.api_request_started_at = time.perf_counter()
    app.logger.info(
        "[api] started method=%s path=%s args=%s",
        request.method,
        request.path,
        dict(request.args),
    )


@app.after_request
def _log_api_request_completed(response):
    if not request.path.startswith("/api/"):
        return response

    started_at = getattr(g, "api_request_started_at", None)
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2) if started_at else None
    if duration_ms is not None:
        response.headers["X-Response-Time-Ms"] = str(duration_ms)

    app.logger.info(
        "[api] completed method=%s path=%s status=%s duration_ms=%s",
        request.method,
        request.path,
        response.status_code,
        duration_ms,
    )
    return response

_SCAN_STATE_LOCK = threading.Lock()
_SCAN_STATE = {
    "running": False,
    "status": "idle",
    "source": None,
    "started_at": None,
    "finished_at": None,
    "project_key": None,
    "board_id": None,
    "error": None,
    "last_result": None,
}

_BACKGROUND_SCAN_THREADS = set()
_BACKGROUND_SCAN_THREADS_LOCK = threading.Lock()
_BACKGROUND_SHUTTING_DOWN = threading.Event()
_LANDING_OVERVIEW_CACHE_LOCK = threading.Lock()
_LANDING_OVERVIEW_CACHE = {
    "cache_key": None,
    "payload": None,
    "built_at": None,
}

# URL of the jira-mcp container's SSE endpoint (set MCP_SERVER_URL env var to override)
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://jira-mcp:8000/sse")

import db as compliance_db
import compliance as compliance_rules
import ai_classify
import ai_draft
import field_rules
import jira_rest
from landing_config import load_landing_config, save_landing_config
from langgraph_jira_login import handle_jira_login
from jira_rag_knowledge import get_jira_guidance
from hierarchy_tracer import HierarchyTracer

compliance_db.init_db()


def _has_runtime_jira_credentials():
    if has_request_context():
        session_auth = session.get("jira_auth") or {}
        if all([
            session_auth.get("url"),
            session_auth.get("username"),
            session_auth.get("password"),
        ]):
            return True

    return all([
        os.environ.get("JIRA_URL"),
        os.environ.get("JIRA_USERNAME"),
        os.environ.get("JIRA_PASSWORD"),
    ])


def _runtime_jira_error_message():
    return (
        "Jira runtime credentials are not available in the current Flask session or process. "
        "Please log in to Jira again from the login page so scans and direct field access can run."
    )

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
        _log_scan_progress("=== run_async error ===", level="error")
        traceback.print_exc()
        real_errors = _flatten(e)
        return {"error": "; ".join(real_errors)}


def _scan_state_snapshot():
    with _SCAN_STATE_LOCK:
        return dict(_SCAN_STATE)


def _track_background_scan_thread(thread):
    with _BACKGROUND_SCAN_THREADS_LOCK:
        _BACKGROUND_SCAN_THREADS.add(thread)


def _untrack_background_scan_thread(thread):
    with _BACKGROUND_SCAN_THREADS_LOCK:
        _BACKGROUND_SCAN_THREADS.discard(thread)


def _log_scan_progress(message, *args, level="info"):
    """Best-effort scan logging that stays quiet during interpreter shutdown."""
    if _BACKGROUND_SHUTTING_DOWN.is_set() or sys.is_finalizing():
        return
    try:
        getattr(app.logger, level)(message, *args)
    except Exception:
        pass


def _landing_cache_key(summary, config):
    return (
        "landing-v2",
        summary.get("run_id"),
        summary.get("checked_at"),
        len(summary.get("active", [])),
        json.dumps(config or {}, sort_keys=True, separators=(",", ":")),
    )


def _get_cached_landing_overview(cache_key):
    with _LANDING_OVERVIEW_CACHE_LOCK:
        if _LANDING_OVERVIEW_CACHE["cache_key"] != cache_key or _LANDING_OVERVIEW_CACHE["payload"] is None:
            return None
        return copy.deepcopy(_LANDING_OVERVIEW_CACHE["payload"])


def _store_cached_landing_overview(cache_key, payload):
    with _LANDING_OVERVIEW_CACHE_LOCK:
        _LANDING_OVERVIEW_CACHE.update({
            "cache_key": cache_key,
            "payload": copy.deepcopy(payload),
            "built_at": time.time(),
        })


def _invalidate_landing_overview_cache(reason="unknown"):
    with _LANDING_OVERVIEW_CACHE_LOCK:
        _LANDING_OVERVIEW_CACHE.update({
            "cache_key": None,
            "payload": None,
            "built_at": None,
        })
    app.logger.info("[landing] cache invalidated reason=%s", reason)


def _fetch_issue_raw_batch(issue_keys, entity_name):
    keys = [key for key in dict.fromkeys(issue_keys) if key]
    if not keys:
        return {}

    started_at = time.perf_counter()
    max_workers = min(CONCURRENT_FETCH_LIMIT, len(keys)) or 1
    results = {}

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"landing-{entity_name}") as executor:
        future_to_key = {executor.submit(_safe_get_issue_raw, key): key for key in keys}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception as e:
                app.logger.warning("[landing] Fallback fetch failed entity=%s key=%s error=%s", entity_name, key, e)
                results[key] = None

    app.logger.info(
        "[landing] Fallback fetch batch entity=%s count=%s duration_ms=%s",
        entity_name,
        len(keys),
        round((time.perf_counter() - started_at) * 1000, 2),
    )
    return results


def _shutdown_background_scans(join_timeout=5.0):
    _BACKGROUND_SHUTTING_DOWN.set()
    with _BACKGROUND_SCAN_THREADS_LOCK:
        threads = list(_BACKGROUND_SCAN_THREADS)

    deadline = time.time() + join_timeout
    for thread in threads:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        if thread.is_alive():
            thread.join(timeout=remaining)


atexit.register(_shutdown_background_scans)


def _set_scan_state(**patch):
    with _SCAN_STATE_LOCK:
        _SCAN_STATE.update(patch)
        return dict(_SCAN_STATE)


def _start_background_scan(project_key=None, board_id=None, source="manual"):
    if _BACKGROUND_SHUTTING_DOWN.is_set() or sys.is_finalizing():
        current = _scan_state_snapshot()
        return {**current, "started": False, "already_running": False, "shutting_down": True}

    current = _scan_state_snapshot()
    if current.get("running"):
        return {**current, "started": False, "already_running": True}

    started_at = __import__("time").time()
    _set_scan_state(
        running=True,
        status="running",
        source=source,
        started_at=started_at,
        finished_at=None,
        project_key=project_key,
        board_id=board_id,
        error=None,
    )

    def _worker():
        current_thread = threading.current_thread()
        try:
            _invalidate_landing_overview_cache(reason=f"scan-start:{source}")
            result = run_async(_run_full_scan(project_key=project_key, board_id=board_id))
            if isinstance(result, dict) and "error" in result:
                _set_scan_state(
                    running=False,
                    status="failed",
                    finished_at=__import__("time").time(),
                    error=result["error"],
                    last_result=None,
                )
                return

            try:
                _get_or_build_landing_overview(force_refresh=True)
            except Exception as warm_error:
                app.logger.warning("[landing] Cache warm failed after scan source=%s error=%s", source, warm_error)

            _set_scan_state(
                running=False,
                status="completed",
                finished_at=__import__("time").time(),
                error=None,
                last_result=result,
            )
        except Exception as e:
            _set_scan_state(
                running=False,
                status="failed",
                finished_at=__import__("time").time(),
                error=str(e),
                last_result=None,
            )
        finally:
            _untrack_background_scan_thread(current_thread)

    worker = threading.Thread(target=_worker, name="background-compliance-scan")
    _track_background_scan_thread(worker)
    worker.start()
    return {**_scan_state_snapshot(), "started": True, "already_running": False}


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
@app.route("/api/issues/<issue_key>", methods=["GET"])
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
@app.route("/api/issues/<issue_key>/transition", methods=["POST"])
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
# Board membership (see _get_allowed_issue_keys below) is the actual scope
# restriction — a CR/Epic/Outcome on an allowed board can link to a Story
# that isn't, which discovery JQL alone can't filter (linkedIssues() has
# no board clause), so linked results get their own explicit membership
# check too, using the same allowed_keys set.
ALLOWED_BOARD_NAMES = [b.strip() for b in os.environ.get("ALLOWED_BOARD_NAMES", "ACC,ACBD").split(",") if b.strip()]
CR_DISCOVERY_JQL = 'filter = Asylum_CRs'
EPIC_DISCOVERY_JQL = 'filter = Asylum_Epics'
OUTCOME_DISCOVERY_JQL = 'filter = Asylum_Outcomes'

# Scrum Team is a filter dimension, not a mandatory field check, so it
# lives here rather than in field_rules.py's CR/EPIC/OUTCOME_FIELDS.
SCRUM_TEAM_FIELD_ID = os.environ.get("SCRUM_TEAM_FIELD_ID", "customfield_21304")

# Discovery-mechanism field IDs, confirmed against real production XML
# exports — kept separate from field_rules.py's CR/EPIC/OUTCOME_FIELDS
# (which drive the field-completeness checks, not discovery) since
# those weren't touched in this pass.
EPIC_LINK_FIELD_ID = os.environ.get("EPIC_LINK_FIELD_ID", "customfield_10006")
PARENT_LINK_FIELD_ID = os.environ.get("PARENT_LINK_FIELD_ID", "customfield_14701")


def _get_allowed_issue_keys(board_id=None, project_key=None):
    """Every issue key currently on the specified board or boards in ALLOWED_BOARD_NAMES.

    - If board_id is provided: fetch issues only from that specific board
    - If project_key is provided: fetch issues from all boards matching that project
    - Otherwise: fetch issues from all boards matching ALLOWED_BOARD_NAMES

    The board membership is the actual source of truth for scoping the scan, since
    board membership isn't something JQL can filter on directly (unlike a project,
    boards aren't encoded in the issue key either, so this has to be a real membership
    lookup, not a JQL clause or a key-prefix check).

    Synchronous (jira_rest uses `requests`) — callers should run this via asyncio.to_thread."""

    allowed_keys = set()

    if board_id:
        # Fetch issues from the specific selected board only
        _log_scan_progress("[scan] fetching issues from selected board %s...", board_id)
        issues = jira_rest.search_board_issues(board_id)
        allowed_keys.update(i.get("key") for i in issues if i.get("key"))
        _log_scan_progress("[scan] found %s issues on board %s", len(allowed_keys), board_id)
    else:
        # Fetch issues from all allowed boards
        all_boards = jira_rest.get_boards(project_key=project_key)
        # Filter to only boards in ALLOWED_BOARD_NAMES
        matching_boards = [b for b in all_boards if b.get("name") in ALLOWED_BOARD_NAMES]

        if not matching_boards:
            if project_key:
                _log_scan_progress(
                    "[scan] WARNING: no boards found for project %s matching %s — check the exact board names in Jira, or set ALLOWED_BOARD_NAMES to override.",
                    project_key,
                    ALLOWED_BOARD_NAMES,
                    level="warning",
                )
            else:
                _log_scan_progress(
                    "[scan] WARNING: no boards found matching %s — check the exact board names in Jira, or set ALLOWED_BOARD_NAMES to override.",
                    ALLOWED_BOARD_NAMES,
                    level="warning",
                )
            return set()

        _log_scan_progress("[scan] fetching issues from %s allowed boards...", len(matching_boards))
        for board in matching_boards:
            issues = jira_rest.search_board_issues(board["id"])
            allowed_keys.update(i.get("key") for i in issues if i.get("key"))

    return allowed_keys

# How many direct-REST fetches (jira_rest.get_issue_raw) run concurrently.
# These are blocking HTTP calls, moved off the event loop via
# asyncio.to_thread and batched — at 10k+ records, doing these one at a
# time sequentially was the dominant cost in scan time. Bounded rather
# than unlimited so we don't hammer Jira / trip rate limits.
CONCURRENT_FETCH_LIMIT = int(os.environ.get("CONCURRENT_FETCH_LIMIT", 8))


class JiraSearchError(RuntimeError):
    """Raised when Jira search returns a transport-successful but unusable payload."""


def _summarize_jira_search_error(text, jql=None):
    raw = str(text or "").strip()
    compact = " ".join(raw.split())
    lowered = compact.lower()
    if "<html" in lowered or "<title>" in lowered:
        title_match = re.search(r"<title>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = re.sub(r"\s+", " ", title_match.group(1)).strip()
            title = title.replace("&mdash;", "—")
        else:
            title = "Jira returned an HTML error page"
        return f"{title}. JQL: {jql}" if jql else title
    if compact:
        return f"Unexpected Jira search payload: {compact[:240]}"
    return f"Empty Jira search response for JQL: {jql}" if jql else "Empty Jira search response"


def _parse_jira_search_page(result, *, jql=None):
    import json as _json
    page_issues = []
    total = None
    saw_json = False
    errors = []

    for block in [b.model_dump() for b in result.content]:
        text = block.get("text")
        if not text:
            continue
        try:
            parsed = _json.loads(text)
        except Exception:
            errors.append(_summarize_jira_search_error(text, jql=jql))
            continue

        saw_json = True
        if isinstance(parsed, dict):
            if parsed.get("error") or parsed.get("errorMessage"):
                errors.append(parsed.get("error") or parsed.get("errorMessage"))
                continue
            page_issues = parsed.get("issues", []) or []
            total = parsed.get("total")

    if errors and not saw_json and not page_issues:
        raise JiraSearchError(errors[0])

    return page_issues, total


async def _search_all_issues11(session, jql, max_pages=250):
    """jira_search only returns one page by default, which silently
    truncates results on any query returning more than a page's worth —
    a real correctness bug at 10k+ record scale, not just a performance
    one. Loops using the tool's next_page_token until exhausted (or
    max_pages, as a safety cap against an infinite loop if something's
    wrong), returning every matching issue."""
    all_issues = []
    page_token = None
    for _ in range(max_pages):
        args = {"jql": jql}
        if page_token:
            args["page_token"] = page_token
        result = await session.call_tool("jira_search", args)
        page_token = None
        page_issues, _ = _parse_jira_search_page(result, jql=jql)
        all_issues.extend(page_issues)
        for block in [b.model_dump() for b in result.content]:
            text = block.get("text")
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except Exception:
                continue
            if isinstance(parsed, dict):
                page_token = parsed.get("next_page_token")
        if not page_token:
            break
    return all_issues

async def _search_all_issues(session, jql, max_pages=250):
    """jira_search paginates via start_at (NOT page_token — that's a
    Cloud-only mechanism per the tool's own schema; this Jira instance
    is Server/DC, which requires start_at). limit (not max_results,
    which the tool schema rejects) controls page size, capped at 50 by
    the tool itself. Loops using start_at + the reported total until
    every matching issue is collected, or max_pages as a safety cap."""
    all_issues = []
    total = None
    for _ in range(max_pages):
        args = {"jql": jql, "limit": 50}
        if all_issues:
            args["start_at"] = len(all_issues)
        result = await session.call_tool("jira_search", args)
        page_issues, page_total = _parse_jira_search_page(result, jql=jql)
        if page_total is not None:
            total = page_total
        if not page_issues:
            break
        all_issues.extend(page_issues)
        if total is not None and len(all_issues) >= total:
            break
    return all_issues
async def _discover_issues(session, base_jql, project_key=None, board_id=None):
    """Finds issues matching base_jql (a type/label filter, e.g.
    CR_DISCOVERY_JQL), optionally narrowed to one board or one project.

    Board scoping uses Jira's separate Agile REST API (via jira_rest,
    run in a thread so the synchronous `requests` call doesn't block the
    event loop) — a board isn't a JQL-queryable concept, so this can't
    reuse the normal MCP search path.

    Project scoping just prepends a project filter onto the existing
    JQL and reuses the normal MCP-based paginated search — no new API
    needed for this one.

    board_id takes priority if both are somehow set, since a board
    already implies a specific project."""
    """ if board_id:
        return await asyncio.to_thread(jira_rest.search_board_issues, board_id, base_jql)
    if project_key:
        scoped_jql = f'project = "{project_key}" AND ({base_jql})'
        return await _search_all_issues(session, scoped_jql) """
    return await _search_all_issues(session, base_jql)


async def _linked_issues_concurrent(session, keys, limit=None):
    """Finds linked issue_search_all_issuess for multiple entities (CRs or Outcomes)
    concurrently instead of one sequential MCP round-trip per entity —
    confirmed as the dominant cost in scan time at any real scale (each
    linkedIssues() search is a full network round-trip to Jira; 9
    sequential searches alone measured ~12s in testing, meaning 1,000+
    entities would take 20+ minutes done one at a time).

    Bounded by a semaphore, same reasoning as _fetch_raw_concurrent.
    Reuses the existing, already-correct _search_all_issues machinery
    per call rather than a new API path — MCP's JSON-RPC transport
    correlates concurrent in-flight requests by ID, so multiple calls
    on one shared session are safe by design, not something bolted on.
    Returns {key: [issues]} — a failed lookup for one key logs and
    returns an empty list for that key rather than aborting the batch."""
    sem = asyncio.Semaphore(limit or CONCURRENT_FETCH_LIMIT)
    results = {}

    async def _one(key):
        async with sem:
            try:
                results[key] = await _search_all_issues(session, f'issue in linkedIssues("{key}")')
            except Exception as e:
                _log_scan_progress("[scan] could not fetch linked issues for %s: %s", key, e, level="warning")
                results[key] = []

    await asyncio.gather(*(_one(k) for k in keys))
    return results


async def _epic_link_siblings_concurrent(session, epic_link_values, limit=None):
    """Finds epic-linked child issues that share the same Epic Link value
    as a CR or Epic. We include Story, Bug, and Change Request issue
    types so the hierarchy and epic popup can show every work item that
    Jira stores directly under the Epic via the classic "Epic Link"
    field, not just Stories.

    This is additive to _linked_issues_concurrent's issue-links search,
    not a replacement — some children may still be found via genuine
    issue-links, so the caller unions both results.

    "Epic Link" is a builtin JQL clause name in Jira (works without
    needing the specific customfield ID in the query text), so this
    reuses the same MCP search path as everything else. Deduped by
    epic_link_values, since multiple CRs commonly share one Epic.
    Returns {epic_link_value: [stories]}."""
    sem = asyncio.Semaphore(limit or CONCURRENT_FETCH_LIMIT)
    results = {}

    async def _one(epic_key):
        async with sem:
            try:
                results[epic_key] = await _search_all_issues(
                    session, f'"Epic Link" = "{epic_key}" AND type in ("Story", "Bug", "Change Request")'
                )
            except Exception as e:
                _log_scan_progress("[scan] could not fetch Epic Link siblings for %s: %s", epic_key, e, level="warning")
                results[epic_key] = []

    await asyncio.gather(*(_one(k) for k in set(epic_link_values) if k))
    return results


async def _fetch_raw_concurrent(keys):
    """Fetch raw Jira data (for field-completeness checks) for multiple
    issues concurrently instead of one blocking call at a time. Each
    fetch runs in a thread (jira_rest uses the synchronous `requests`
    library) so it doesn't block the event loop while waiting; a
    semaphore caps how many run at once. Returns {key: raw_json_or_None} —
    a None value means that specific fetch failed and was logged, not
    that the whole batch failed."""
    sem = asyncio.Semaphore(CONCURRENT_FETCH_LIMIT)
    results = {}

    async def _one(key):
        async with sem:
            try:
                results[key] = await asyncio.to_thread(jira_rest.get_issue_raw, key)
            except Exception as e:
                _log_scan_progress("[jira_rest] could not fetch %s: %s", key, e, level="warning")
                results[key] = None

    await asyncio.gather(*(_one(k) for k in keys))
    return results


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
        _log_scan_progress("[ai_classify] could not classify %s status %r: %s", status_type, raw_status, e, level="warning")
        return
    compliance_rules.learn_alias(status_type, raw_status, result["target"])
    compliance_db.save_status_mapping(
        status_type, raw_status, result["target"], "ai", result.get("reasoning", ""), result.get("confidence", "")
    )
    _log_scan_progress(
        "[ai_classify] learned: %s %r -> %r (%s)",
        status_type,
        raw_status,
        result["target"],
        result.get("confidence"),
    )


async def _run_full_scan(project_key=None, board_id=None):
    import json as _json
    import uuid as _uuid
    import datetime
    import time as _time

    scan_start = _time.time()
    field_run_id = str(_uuid.uuid4())
    results = []
    field_findings_by_entity = []  # [(entity_type, entity_key, entity_status, findings)]
    epic_to_cr_entries = {}  # epic_key -> [(cr_key, cr_status), ...]
    outcome_to_epic_entries = {}  # outcome_key -> [(epic_key, epic_status), ...] — an Epic's parent is an Outcome
    outcome_status_by_key = {}
    epic_results = []
    outcome_results = []
    outcome_epic_results = []
    issue_summaries = {}  # issue_key -> summary/title, collected from every discovery point below
    issue_scrum_teams = {}  # issue_key -> Scrum Team value, collected from CR/Epic/Outcome raw data
    issue_labels = {}
    issue_relationships = set()

    app.logger.info(
        "[scan] Starting full scan: project_key=%s board_id=%s",
        project_key,
        board_id,
    )
    _log_scan_progress("DEBUG MCP_SERVER_URL = %r", MCP_SERVER_URL)

    # Clear all old scan data to ensure fresh results each time
    #cleared = compliance_db.clear_all_scan_data()
    #app.logger.info("[scan] Cleared %d old scan runs to start fresh", cleared["cleared_runs"])
    #print(f"[scan] cleared {cleared['cleared_runs']} old runs for fresh data")

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            _log_scan_progress("[scan] fetching board membership...")
            allowed_keys = await asyncio.to_thread(_get_allowed_issue_keys, board_id, project_key)
            _log_scan_progress("[scan] %s issue keys to process", len(allowed_keys))
# Outcome field-completeness + Story <- Outcome phase alignment
            outcomes = await _discover_issues(session, OUTCOME_DISCOVERY_JQL, project_key, board_id)

            _log_scan_progress("[scan] discovered %s Outcomes, fetching field data concurrently...", len(outcomes))
            app.logger.info(
                "[scan] Discovered %d Outcomes before board filtering (project=%s board_id=%s)",
                len(outcomes),
                project_key,
                board_id,
            )
            outcomes = [o for o in outcomes]
            app.logger.info("[scan] Outcomes after board/allowed filtering: %d", len(outcomes))

            issue_summaries.update({o.get("key"): o.get("summary") for o in outcomes})
            _log_scan_progress("[scan] discovered %s Outcomes, fetching field data concurrently...", len(outcomes))
            outcome_raw_by_key = await _fetch_raw_concurrent([o.get("key") for o in outcomes])
            _log_scan_progress("[scan] fetching linked stories for %s Outcomes concurrently...", len(outcomes))
            outcome_linked_by_key = await _linked_issues_concurrent(session, [o.get("key") for o in outcomes])
            for _linked_list in outcome_linked_by_key.values():
                issue_summaries.update({i.get("key"): i.get("summary") for i in _linked_list})
            outcome_field_map = _effective_field_map("outcome")

            for outcome in outcomes:
                outcome_key = outcome.get("key")
                outcome_status = (outcome.get("status") or {}).get("name")
                outcome_status_by_key[outcome_key] = outcome_status
                _ensure_classified("outcome", outcome_status)

                raw = outcome_raw_by_key.get(outcome_key)
                if raw is not None:
                    outcome_findings = field_rules.check_outcome_fields(raw.get("fields", {}), outcome_status, field_map=outcome_field_map)
                    field_findings_by_entity.append(("outcome", outcome_key, outcome_status, outcome_findings))
                    scrum_team = field_rules._value(raw.get("fields", {}), SCRUM_TEAM_FIELD_ID)
                    if scrum_team:
                        issue_scrum_teams[outcome_key] = scrum_team
                    issue_labels[outcome_key] = raw.get("fields", {}).get("labels") or []
                elif outcome.get("labels"):
                    issue_labels[outcome_key] = outcome.get("labels") or []

                outcome_linked = [i for i in outcome_linked_by_key.get(outcome_key, []) if i.get("key") in allowed_keys]

                for issue in outcome_linked:
                    if (issue.get("issue_type") or {}).get("name") != "Story":
                        continue
                    story_key = issue.get("key")
                    story_status = (issue.get("status") or {}).get("name")
                    verdict = compliance_rules.evaluate_outcome(story_status, outcome_status)
                    outcome_results.append({
                        "pair_type": "story_outcome",
                        "cr_key": story_key, "cr_status": story_status,
                        "story_key": outcome_key, "story_status": outcome_status,
                        "compliant": verdict["compliant"], "reason": verdict["reason"],
                        "severity": verdict["severity"], "score": verdict["score"],
                    })

            crs = await _discover_issues(session, CR_DISCOVERY_JQL, project_key, board_id)
            app.logger.info(
                "[scan] Discovered %d CRs before board filtering (project=%s board_id=%s)",
                len(crs),
                project_key,
                board_id,
            )
            crs = [c for c in crs]
            app.logger.info(
                "[scan] CRs after board/allowed filtering: %d (allowed_keys count=%d)",
                len(crs),
                len(allowed_keys),
            )
            issue_summaries.update({c.get("key"): c.get("summary") for c in crs})
            _log_scan_progress("[scan] discovered %s CRs, fetching field data concurrently...", len(crs))
            cr_raw_by_key = await _fetch_raw_concurrent([cr.get("key") for cr in crs])
            _log_scan_progress("[scan] fetching linked stories for %s CRs concurrently...", len(crs))
            cr_linked_by_key = await _linked_issues_concurrent(session, [cr.get("key") for cr in crs])
            for _linked_list in cr_linked_by_key.values():
                issue_summaries.update({i.get("key"): i.get("summary") for i in _linked_list})

            # Real production data confirmed CR and Story are siblings
            # under one Epic (both use the classic Epic Link field), not
            # linked to each other via issue-links — so also fetch
            # sibling stories per CR's Epic Link value, unioned below
            # with the issue-links search above rather than replacing it.
            cr_epic_links = {
                cr.get("key"): (cr_raw_by_key.get(cr.get("key")) or {}).get("fields", {}).get(EPIC_LINK_FIELD_ID)
                for cr in crs
            }
            _log_scan_progress("[scan] fetching Epic-Link sibling stories for %s unique epics...", len(set(cr_epic_links.values())))
            epic_link_siblings_by_epic = await _epic_link_siblings_concurrent(session, cr_epic_links.values())
            for _sibling_list in epic_link_siblings_by_epic.values():
                issue_summaries.update({i.get("key"): i.get("summary") for i in _sibling_list})

            cr_field_map = _effective_field_map("cr")

            for cr in crs:
                cr_key = cr.get("key")
                cr_status = (cr.get("status") or {}).get("name")
                _ensure_classified("cr", cr_status)

                # Field-completeness check for this CR — uses the raw data
                # already fetched concurrently above, no extra blocking call
                # here. Also reused to find the CR's Epic Link for the
                # Epic<-CR bottleneck check below.
                raw = cr_raw_by_key.get(cr_key)
                if raw is not None:
                    raw_fields = raw.get("fields", {})
                    cr_findings = field_rules.check_cr_fields(raw_fields, cr_status, field_map=cr_field_map)
                    field_findings_by_entity.append(("cr", cr_key, cr_status, cr_findings))
                    scrum_team = field_rules._value(raw_fields, SCRUM_TEAM_FIELD_ID)
                    if scrum_team:
                        issue_scrum_teams[cr_key] = scrum_team

                    epic_key = None
                    parent = raw_fields.get("parent")
                    if parent and isinstance(parent, dict):
                        epic_key = parent.get("key")
                    if not epic_key:
                        epic_key = raw_fields.get(EPIC_LINK_FIELD_ID)
                    if epic_key:
                        epic_to_cr_entries.setdefault(epic_key, []).append((cr_key, cr_status))
                        issue_relationships.add((cr_key, epic_key, "epic_contains_cr"))

                issuelinks_stories = cr_linked_by_key.get(cr_key, [])
                sibling_stories = epic_link_siblings_by_epic.get(cr_epic_links.get(cr_key), [])
                seen_keys = set()
                linked = []
                for i in issuelinks_stories + sibling_stories:
                    k = i.get("key")
                    if k and k not in seen_keys and k in allowed_keys:
                        seen_keys.add(k)
                        linked.append(i)

                for issue in linked:
                    if (issue.get("issue_type") or {}).get("name") != "Story":
                        continue
                    story_key = issue.get("key")
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
                        "story_key": story_key, "story_status": story_status,
                        "compliant": verdict["compliant"], "reason": verdict["reason"],
                        "severity": verdict["severity"], "score": verdict["score"],
                    })

            # Epic <- CR bottleneck check, using the Epic Links collected above.
            epics = await _discover_issues(session, EPIC_DISCOVERY_JQL, project_key, board_id)
            app.logger.info(
                "[scan] Discovered %d Epics before board filtering (project=%s board_id=%s)",
                len(epics),
                project_key,
                board_id,
            )
            epics = [e for e in epics]
            app.logger.info("[scan] Epics after board/allowed filtering: %d", len(epics))
            issue_summaries.update({e.get("key"): e.get("summary") for e in epics})
            _log_scan_progress("[scan] discovered %s Epics, fetching field data concurrently...", len(epics))
            epic_raw_by_key = await _fetch_raw_concurrent([epic.get("key") for epic in epics])
            epic_field_map = _effective_field_map("epic")

            for epic in epics:
                epic_key = epic.get("key")
                epic_status = (epic.get("status") or {}).get("name")
                _ensure_classified("epic", epic_status)

                raw = epic_raw_by_key.get(epic_key)
                if raw is not None:
                    epic_fields = raw.get("fields", {})
                    epic_findings = field_rules.check_epic_fields(epic_fields, epic_status, field_map=epic_field_map)
                    field_findings_by_entity.append(("epic", epic_key, epic_status, epic_findings))
                    scrum_team = field_rules._value(epic_fields, SCRUM_TEAM_FIELD_ID)
                    if scrum_team:
                        issue_scrum_teams[epic_key] = scrum_team

                    # An Epic's parent is an Outcome — confirmed against
                    # real production data that this is via the "Parent
                    # Link" custom field (Advanced Roadmaps' parent-link
                    # mechanism), not the native "parent" field. Native
                    # parent kept as a fallback in case some issues do
                    # use it.
                    outcome_key = None
                    parent = epic_fields.get("parent")
                    if parent and isinstance(parent, dict):
                        outcome_key = parent.get("key")
                    if not outcome_key:
                        outcome_key = epic_fields.get(PARENT_LINK_FIELD_ID)
                    if outcome_key:
                        outcome_to_epic_entries.setdefault(outcome_key, []).append((epic_key, epic_status))
                        issue_relationships.add((epic_key, outcome_key, "outcome_contains_epic"))

                cr_entries = epic_to_cr_entries.get(epic_key, [])
                verdict = compliance_rules.evaluate_epic(epic_status, cr_entries)
                if verdict["bottleneck_cr_key"] is None:
                    continue  # no linked CRs — nothing to compare, skip rather than record a hollow row
                epic_results.append({
                    "pair_type": "epic_cr",
                    "cr_key": verdict["bottleneck_cr_key"], "cr_status": verdict["bottleneck_cr_status"],
                    "story_key": epic_key, "story_status": epic_status,
                    "compliant": verdict["compliant"], "reason": verdict["reason"],
                    "severity": verdict["severity"], "score": verdict["score"],
                })

            # Outcome <- Epic bottleneck checks must run AFTER the Epic scan
            # above, because that's where outcome_to_epic_entries gets filled.
            # Running this inside the initial Outcomes loop always saw an empty
            # mapping and therefore produced zero rows even when outcomes were
            # discovered successfully.
            for outcome_key, outcome_status in outcome_status_by_key.items():
                epic_entries = outcome_to_epic_entries.get(outcome_key, [])
                epic_verdict = compliance_rules.evaluate_outcome_epic(outcome_status, epic_entries)
                if epic_verdict["bottleneck_epic_key"] is None:
                    continue
                outcome_epic_results.append({
                    "pair_type": "outcome_epic",
                    "cr_key": epic_verdict["bottleneck_epic_key"], "cr_status": epic_verdict["bottleneck_epic_status"],
                    "story_key": outcome_key, "story_status": outcome_status,
                    "compliant": epic_verdict["compliant"], "reason": epic_verdict["reason"],
                    "severity": epic_verdict["severity"], "score": epic_verdict["score"],
                })


    run_id = compliance_db.save_run(results)
    if epic_results:
        compliance_db.save_additional_checks(run_id, epic_results, "epic_cr")
    if outcome_results:
        compliance_db.save_additional_checks(run_id, outcome_results, "story_outcome")
    if outcome_epic_results:
        compliance_db.save_additional_checks(run_id, outcome_epic_results, "outcome_epic")
    compliance_db.save_issue_summaries(issue_summaries)
    compliance_db.save_issue_scrum_teams(issue_scrum_teams)
    compliance_db.save_issue_labels(issue_labels)
    compliance_db.save_issue_relationships(issue_relationships)
    for entity_type, entity_key, entity_status, findings in field_findings_by_entity:
        compliance_db.save_field_findings(field_run_id, entity_type, entity_key, entity_status, findings)

    elapsed = _time.time() - scan_start
    _log_scan_progress(
        "[scan] completed in %.1fs — %s CRs, %s story checks, %s epic checks, %s story-outcome checks, %s outcome-epic checks, %s entities field-checked",
        elapsed,
        len(set(r["cr_key"] for r in results)),
        len(results),
        len(epic_results),
        len(outcome_results),
        len(outcome_epic_results),
        len(field_findings_by_entity),
    )

    return {
        "crs_scanned": len(set(r["cr_key"] for r in results)),
        "stories_scanned": len(results),
        "epics_scanned": len(epics),
        "outcomes_scanned": len(outcome_status_by_key),
        "story_outcome_pairs_scanned": len(outcome_results),
        "outcome_epic_pairs_scanned": len(outcome_epic_results),
        "field_entities_scanned": len(field_findings_by_entity),
        "field_findings_total": sum(len(f) for _, _, _, f in field_findings_by_entity),
        "scan_seconds": round(elapsed, 1),
    }


@app.route("/api/issues/<issue_key>/description", methods=["GET"])
def get_issue_description(issue_key):
    """Fetches an issue's full title and description live (not from the
    stored scan data) — used by the title-click popup. Description isn't
    captured during the bulk scan (would bloat the DB for something only
    needed occasionally), so this is a light on-demand fetch instead."""
    try:
        raw = jira_rest.get_issue_raw(issue_key)
    except Exception as e:
        return jsonify({"error": f"Could not fetch issue: {e}"}), 500
    fields = raw.get("fields", {})
    return jsonify({
        "issue_key": issue_key,
        "summary": fields.get("summary"),
        "description": field_rules._description_text(fields.get("description")),
    })


@app.route("/api/issues/summaries", methods=["GET"])
def get_issue_summaries():
    keys_param = request.args.get("keys")
    keys = [k.strip() for k in keys_param.split(",") if k.strip()] if keys_param else None
    return jsonify(compliance_db.get_issue_summaries(keys))


@app.route("/api/issues/scrum-teams", methods=["GET"])
def get_issue_scrum_teams():
    keys_param = request.args.get("keys")
    keys = [k.strip() for k in keys_param.split(",") if k.strip()] if keys_param else None
    return jsonify(compliance_db.get_issue_scrum_teams(keys))


@app.route("/api/pairs/epic-cr", methods=["GET"])
def get_epic_cr_findings():
    run_id = compliance_db.latest_run_id()
    if not run_id:
        return jsonify([])
    return jsonify(compliance_db.get_pair_findings(run_id, "epic_cr"))


@app.route("/api/pairs/outcome-epic", methods=["GET"])
def get_outcome_epic_findings():
    run_id = compliance_db.latest_run_id()
    if not run_id:
        return jsonify([])
    return jsonify(compliance_db.get_pair_findings(run_id, "outcome_epic", include_compliant=True))


@app.route("/api/pairs/story-outcome", methods=["GET"])
def get_story_outcome_findings():
    run_id = compliance_db.latest_run_id()
    if not run_id:
        return jsonify([])
    return jsonify(compliance_db.get_pair_findings(run_id, "story_outcome", include_compliant=True))


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
        findings = field_rules.check_cr_fields(raw.get("fields", {}), status_name, field_map=_effective_field_map("cr"))
    else:
        findings = field_rules.check_outcome_fields(raw.get("fields", {}), status_name, field_map=_effective_field_map("outcome"))

    return jsonify({"issue_key": issue_key, "status": status_name, "findings": findings})


_FIELD_MAPS = {
    "cr": (field_rules.CR_FIELDS, field_rules.CR_FIELD_LABELS, field_rules.CR_FIELD_TYPES),
    "epic": (field_rules.EPIC_FIELDS, field_rules.EPIC_FIELD_LABELS, field_rules.EPIC_FIELD_TYPES),
    "outcome": (field_rules.OUTCOME_FIELDS, field_rules.OUTCOME_FIELD_LABELS, field_rules.OUTCOME_FIELD_TYPES),
}

_CHECK_FUNCS = {
    "cr": field_rules.check_cr_fields,
    "epic": field_rules.check_epic_fields,
    "outcome": field_rules.check_outcome_fields,
}


def _effective_field_map(entity_type):
    """Currently just the hardcoded defaults — AI-assisted field mapping
    discovery was built but not deployed, so this is a plain passthrough
    for now, kept as a single function so re-enabling that feature later
    only means changing this one place, not every call site again."""
    return _FIELD_MAPS[entity_type][0]


def _safe_get_issue_raw(issue_key):
    if not issue_key:
        return None
    try:
        return jira_rest.get_issue_raw(issue_key)
    except Exception as e:
        app.logger.warning("[landing] Could not fetch raw issue for %s: %s", issue_key, e)
        return None


OUTCOME_LABEL_ORDER = [
    "AHRO",
    "ASRA",
    "Run & Sustain(SR2L)",
    "ASP",
    "PMP",
    "CWC",
    "Other",
]


def _normalized_token(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _labels_match_any(labels, needles):
    normalized_labels = [_normalized_token(label) for label in (labels or []) if str(label).strip()]
    normalized_needles = [_normalized_token(needle) for needle in needles if str(needle).strip()]
    return any(
        needle and any(needle in label for label in normalized_labels)
        for needle in normalized_needles
    )


def _title_contains(title_text, needle):
    return _normalized_token(needle) in _normalized_token(title_text)


def _outcome_group_label(outcome_labels, summary_text=None):
    labels = list(outcome_labels or [])
    title_text = summary_text or ""

    if _labels_match_any(labels, ["AHRO"]) or _title_contains(title_text, "AHRO"):
        return "AHRO"
    if _labels_match_any(labels, ["ASRA"]) or _title_contains(title_text, "ASRA"):
        return "ASRA"
    if _labels_match_any(labels, ["runandsustain", "sr2l"]) or _title_contains(title_text, "Run & Sustain(SR2L)"):
        return "Run & Sustain(SR2L)"
    if _labels_match_any(labels, ["ASP"]):
        return "ASP"
    if _labels_match_any(labels, ["PMP"]):
        return "PMP"
    if _labels_match_any(labels, ["CWC"]):
        return "CWC"
    return "Other"


def _node_compliance_rate(issue_count, minimum_if_issues=55):
    if issue_count <= 0:
        return 100
    return max(minimum_if_issues, 100 - min(48, issue_count * 8))


def _issue_severity_rank(severity):
    return {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}.get(severity, 0)


def _next_action_for_issue(issue_category, severity, reason):
    category = (issue_category or "").lower()
    reason = (reason or "").lower()
    if "field" in category:
        return "Update required field"
    if "pr" in category:
        return "Confirm PR / slot evidence"
    if severity in ("Critical", "High") or "behind" in reason:
        return "Escalate in scrum sync"
    if "blocked" in reason:
        return "Resolve dependency"
    return "Review child status"


def _build_landing_overview(summary=None, config=None):
    build_started_at = time.perf_counter()
    config = config if config is not None else load_landing_config()
    summary = summary if summary is not None else compliance_db.get_latest_summary()

    metadata_started_at = time.perf_counter()
    trend = compliance_db.get_trend(7)
    field_findings = compliance_db.get_latest_field_findings()
    issue_summaries = compliance_db.get_issue_summaries()
    scrum_teams = compliance_db.get_issue_scrum_teams()
    issue_labels = compliance_db.get_issue_labels()
    relationships = compliance_db.get_issue_relationships()
    run_id = summary.get("run_id")

    app.logger.info(
        "[landing] Building overview run_id=%s crs_checked=%s stories_checked=%s non_compliant=%s issue_summaries=%s field_findings=%s",
        run_id,
        summary.get("crs_checked"),
        summary.get("stories_checked"),
        summary.get("non_compliant"),
        len(issue_summaries),
        len(field_findings),
    )
    app.logger.info(
        "[landing] Cached metadata loaded labels=%s relationships=%s",
        len(issue_labels),
        len(relationships),
    )
    app.logger.info(
        "[landing] Metadata fetch complete run_id=%s duration_ms=%s",
        run_id,
        round((time.perf_counter() - metadata_started_at) * 1000, 2),
    )

    reason_counts, severity_counts, cr_counts = {}, {}, {}
    for row in summary.get("active", []):
        if row.get("reason"):
            reason_counts[row["reason"]] = reason_counts.get(row["reason"], 0) + 1
        if row.get("severity"):
            severity_counts[row["severity"]] = severity_counts.get(row["severity"], 0) + 1
        if row.get("cr_key"):
            cr_counts[row["cr_key"]] = cr_counts.get(row["cr_key"], 0) + 1
    breakdown = {"by_reason": reason_counts, "by_severity": severity_counts, "by_cr": cr_counts}

    response = {
        "summary": summary,
        "kpis": {
            "overall_compliance": summary.get("compliance_rate") or 0,
            "status_mismatches": 0,
            "field_gaps": len(field_findings),
            "broken_hierarchy_links": 0,
            "pr_missing_against_cr": 0,
        },
        "filters": {
            "labels": [],
            "scrum_teams": sorted({team for team in scrum_teams.values() if team}),
            "severities": ["Critical", "High", "Medium", "Low"],
        },
        "hierarchy": [],
        "recent_issues": [],
        "compliance_matrix": [],
        "rule_metrics": [],
        "trend": trend,
        "breakdown": breakdown,
        "config": config,
        "agentic_ai": {
            "stack": [
                "LangGraph orchestration",
                "LangChain-style reasoning patterns",
                "RAG guidance for Jira governance",
                "Human-in-the-loop overrides",
            ],
            "notes": [
                "Rule cards are configuration-driven so UI impact stays low when policies change.",
                "PR / Slot evidence is scaffolded for a future connector and currently reports zero.",
            ],
        },
        "data_notes": [],
    }

    if not run_id:
        app.logger.warning("[landing] No scan run data available. Returning empty landing overview.")
        response["data_notes"].append("No compliance scan data is available yet. Run a scan to populate the landing page.")
        response["rule_metrics"] = [
            {
                "key": rule.get("key"),
                "label": rule.get("label"),
                "description": rule.get("description"),
                "severity": rule.get("severity"),
                "enabled": bool(rule.get("enabled", True)),
                "count": 0,
                "metric_key": rule.get("metric_key"),
            }
            for rule in config.get("rules", [])
        ]
        return response

    pair_rows_started_at = time.perf_counter()
    cr_story_rows = compliance_db.get_pair_findings(run_id, "cr_story", include_compliant=True)
    epic_rows = compliance_db.get_pair_findings(run_id, "epic_cr", include_compliant=True)
    outcome_story_rows = compliance_db.get_pair_findings(run_id, "story_outcome", include_compliant=True)
    outcome_epic_rows = compliance_db.get_pair_findings(run_id, "outcome_epic", include_compliant=True)

    app.logger.info(
        "[landing] Pair counts run_id=%s cr_story=%s epic_cr=%s story_outcome=%s outcome_epic=%s",
        run_id,
        len(cr_story_rows),
        len(epic_rows),
        len(outcome_story_rows),
        len(outcome_epic_rows),
    )
    app.logger.info(
        "[landing] Pair row fetch complete run_id=%s duration_ms=%s",
        run_id,
        round((time.perf_counter() - pair_rows_started_at) * 1000, 2),
    )

    findings_by_entity = {}
    entity_keys_by_type = {"cr": set(), "epic": set(), "outcome": set(), "story": set()}
    status_by_entity = {}
    for finding in field_findings:
        findings_by_entity.setdefault(finding.get("entity_key"), []).append(finding)
        entity_type = finding.get("entity_type")
        entity_key = finding.get("entity_key")
        if entity_type in entity_keys_by_type and entity_key:
            entity_keys_by_type[entity_type].add(entity_key)
        if entity_key and finding.get("entity_status") and entity_key not in status_by_entity:
            status_by_entity[entity_key] = finding.get("entity_status")

    story_outcome_by_story = {
        row.get("cr_key"): row
        for row in outcome_story_rows
        if row.get("cr_key") and row.get("story_key")
    }

    story_rows_by_cr = {}
    for row in cr_story_rows:
        story_rows_by_cr.setdefault(row.get("cr_key"), []).append(row)

    cr_relationships = [
        rel for rel in relationships
        if rel.get("relationship_type") == "epic_contains_cr"
    ]
    epic_relationships = [
        rel for rel in relationships
        if rel.get("relationship_type") == "outcome_contains_epic"
    ]

    cr_keys = sorted({row.get("cr_key") for row in cr_story_rows if row.get("cr_key")} |
                     {row.get("cr_key") for row in epic_rows if row.get("cr_key")} |
                     entity_keys_by_type["cr"])
    cr_raw_by_key = {}
    cr_status_by_key = {
        key: next((row.get("cr_status") for row in story_rows_by_cr.get(key, []) if row.get("cr_status")), None)
        or status_by_entity.get(key)
        for key in cr_keys
    }

    epic_to_crs = {}
    cr_to_epic = {}
    epic_keys = ({row.get("story_key") for row in epic_rows if row.get("story_key")} |
                 {row.get("cr_key") for row in outcome_epic_rows if row.get("cr_key")} |
                 entity_keys_by_type["epic"])
    for rel in cr_relationships:
        cr_key = rel.get("child_key")
        epic_key = rel.get("parent_key")
        if cr_key not in cr_keys or not epic_key:
            continue
        cr_to_epic[cr_key] = epic_key
        epic_keys.add(epic_key)
        epic_to_crs.setdefault(epic_key, []).append(cr_key)

    unresolved_cr_keys = [key for key in cr_keys if key not in cr_to_epic]
    if unresolved_cr_keys:
        app.logger.info("[landing] Fallback Jira fetch for missing CR->Epic links count=%s", len(unresolved_cr_keys))
        cr_raw_by_key = _fetch_issue_raw_batch(unresolved_cr_keys, "cr")
        for cr_key, raw in cr_raw_by_key.items():
            fields = (raw or {}).get("fields", {})
            epic_key = None
            parent = fields.get("parent")
            if isinstance(parent, dict):
                epic_key = parent.get("key")
            if not epic_key:
                epic_key = fields.get(EPIC_LINK_FIELD_ID)
            if epic_key:
                cr_to_epic[cr_key] = epic_key
                epic_keys.add(epic_key)
                epic_to_crs.setdefault(epic_key, []).append(cr_key)
            if not cr_status_by_key.get(cr_key):
                cr_status_by_key[cr_key] = ((fields.get("status") or {}).get("name"))

    epic_status_by_key = {
        key: next((row.get("story_status") for row in epic_rows if row.get("story_key") == key and row.get("story_status")), None)
        or next((row.get("cr_status") for row in outcome_epic_rows if row.get("cr_key") == key and row.get("cr_status")), None)
        or status_by_entity.get(key)
        for key in sorted(k for k in epic_keys if k)
    }
    outcome_keys = ({row.get("story_key") for row in outcome_story_rows if row.get("story_key")} |
                    {row.get("story_key") for row in outcome_epic_rows if row.get("story_key")} |
                    entity_keys_by_type["outcome"])
    outcome_to_epics = {}

    epic_to_outcome = {
        rel.get("child_key"): rel.get("parent_key")
        for rel in epic_relationships
        if rel.get("child_key") and rel.get("parent_key")
    }

    epic_raw_by_key = {}
    unresolved_epic_keys = [key for key in sorted(k for k in epic_keys if k) if key not in epic_to_outcome]
    if unresolved_epic_keys:
        app.logger.info("[landing] Fallback Jira fetch for missing Epic->Outcome links count=%s", len(unresolved_epic_keys))
        epic_raw_by_key = _fetch_issue_raw_batch(unresolved_epic_keys, "epic")
        for epic_key, raw in epic_raw_by_key.items():
            fields = (raw or {}).get("fields", {})
            outcome_key = None
            parent = fields.get("parent")
            if isinstance(parent, dict):
                outcome_key = parent.get("key")
            if not outcome_key:
                outcome_key = fields.get(PARENT_LINK_FIELD_ID)
            if outcome_key:
                epic_to_outcome[epic_key] = outcome_key
            if not epic_status_by_key.get(epic_key):
                epic_status_by_key[epic_key] = ((fields.get("status") or {}).get("name"))

    for epic_key in sorted(k for k in epic_keys if k):
        outcome_key = epic_to_outcome.get(epic_key)
        if not outcome_key:
            inferred_counts = {}
            for cr_key in epic_to_crs.get(epic_key, []):
                for story_row in story_rows_by_cr.get(cr_key, []):
                    inferred = (story_outcome_by_story.get(story_row.get("story_key")) or {}).get("story_key")
                    if inferred:
                        inferred_counts[inferred] = inferred_counts.get(inferred, 0) + 1
            if inferred_counts:
                outcome_key = sorted(inferred_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
        if outcome_key:
            outcome_keys.add(outcome_key)
            outcome_to_epics.setdefault(outcome_key, []).append(epic_key)

    outcome_status_by_key = {
        key: next((row.get("story_status") for row in outcome_story_rows if row.get("story_key") == key and row.get("story_status")), None)
        or next((row.get("story_status") for row in outcome_epic_rows if row.get("story_key") == key and row.get("story_status")), None)
        or status_by_entity.get(key)
        for key in sorted(k for k in outcome_keys if k)
    }

    unresolved_outcome_keys = [key for key in sorted(k for k in outcome_keys if k) if not issue_labels.get(key) or not outcome_status_by_key.get(key)]
    outcome_raw_by_key = {}
    if unresolved_outcome_keys:
        app.logger.info("[landing] Fallback Jira fetch for missing Outcome metadata count=%s", len(unresolved_outcome_keys))
        outcome_raw_by_key = _fetch_issue_raw_batch(unresolved_outcome_keys, "outcome")
        for outcome_key, raw in outcome_raw_by_key.items():
            fields = (raw or {}).get("fields", {})
            if not issue_labels.get(outcome_key):
                issue_labels[outcome_key] = fields.get("labels") or []
            if not outcome_status_by_key.get(outcome_key):
                outcome_status_by_key[outcome_key] = ((fields.get("status") or {}).get("name"))

    app.logger.info(
        "[landing] Seeded hierarchy entity counts crs=%s epics=%s outcomes=%s cached_links=%s",
        len(cr_keys),
        len(epic_keys),
        len(outcome_keys),
        len(cr_relationships) + len(epic_relationships),
    )

    def _entity_label(entity_type):
        mapping = {
            "cr": "CR",
            "story": "Story",
            "epic": "Epic",
            "outcome": "Outcome",
            "label": "Label",
            "cr/story": "CR / Story",
        }
        normalized = str(entity_type or "").strip().lower()
        return mapping.get(normalized, str(entity_type or "Issue"))

    def _extract_assignee_name(raw):
        assignee = (((raw or {}).get("fields") or {}).get("assignee"))
        if not isinstance(assignee, dict):
            return None
        return assignee.get("displayName") or assignee.get("name") or assignee.get("emailAddress")

    raw_issue_cache = {}
    raw_issue_cache.update({key: value for key, value in cr_raw_by_key.items() if value})
    raw_issue_cache.update({key: value for key, value in epic_raw_by_key.items() if value})
    raw_issue_cache.update({key: value for key, value in outcome_raw_by_key.items() if value})

    assignee_source_keys = sorted({key for key in list(cr_keys) + list(epic_keys) + list(outcome_keys) if key})
    missing_assignee_keys = [key for key in assignee_source_keys if key not in raw_issue_cache]
    if missing_assignee_keys:
        raw_issue_cache.update({
            key: value for key, value in _fetch_issue_raw_batch(missing_assignee_keys, "assignee-metadata").items()
            if value
        })

    assignee_by_key = {
        key: _extract_assignee_name(raw)
        for key, raw in raw_issue_cache.items()
        if _extract_assignee_name(raw)
    }

    story_status_rows_by_story = {}
    cr_status_rows_by_cr = {}
    epic_status_rows_by_epic = {}
    outcome_status_rows_by_outcome = {}

    for row in cr_story_rows:
        if not row.get("compliant"):
            story_status_rows_by_story.setdefault(row.get("story_key"), []).append(row)
            cr_status_rows_by_cr.setdefault(row.get("cr_key"), []).append(row)

    for row in epic_rows:
        if not row.get("compliant"):
            epic_status_rows_by_epic.setdefault(row.get("story_key"), []).append(row)

    for row in outcome_story_rows:
        if not row.get("compliant"):
            outcome_status_rows_by_outcome.setdefault(row.get("story_key"), []).append((row, "story"))

    for row in outcome_epic_rows:
        if not row.get("compliant"):
            outcome_status_rows_by_outcome.setdefault(row.get("story_key"), []).append((row, "epic"))

    def _build_status_detail(issue_key, issue_type, issue_status, related_key, related_type, related_status, row, source):
        return {
            "issueKey": issue_key,
            "issueType": issue_type,
            "issueSummary": issue_summaries.get(issue_key, issue_key),
            "issueStatus": issue_status,
            "relatedKey": related_key,
            "relatedType": related_type,
            "relatedSummary": issue_summaries.get(related_key, related_key),
            "relatedStatus": related_status,
            "severity": row.get("severity"),
            "reason": row.get("reason"),
            "source": source,
        }

    def _field_issue_details(issue_key):
        return [
            {
                "issueKey": issue_key,
                "issueType": _entity_label(finding.get("entity_type")),
                "issueSummary": issue_summaries.get(issue_key, issue_key),
                "issueStatus": finding.get("entity_status"),
                "field": finding.get("field"),
                "severity": finding.get("severity"),
                "message": finding.get("message"),
            }
            for finding in findings_by_entity.get(issue_key, [])
        ]

    def _status_issue_total(children, detail_key):
        total = []
        for child in children:
            total.extend(child.get(detail_key, []))
        return total

    story_issue_counts, cr_direct_issue_counts = {}, {}
    epic_direct_issue_counts, outcome_direct_issue_counts = {}, {}
    for row in cr_story_rows:
        if not row.get("compliant"):
            story_issue_counts[row.get("story_key")] = story_issue_counts.get(row.get("story_key"), 0) + 1
            cr_direct_issue_counts[row.get("cr_key")] = cr_direct_issue_counts.get(row.get("cr_key"), 0) + 1
    for row in epic_rows:
        if not row.get("compliant"):
            epic_direct_issue_counts[row.get("story_key")] = epic_direct_issue_counts.get(row.get("story_key"), 0) + 1
    for row in outcome_story_rows:
        if not row.get("compliant"):
            outcome_direct_issue_counts[row.get("story_key")] = outcome_direct_issue_counts.get(row.get("story_key"), 0) + 1
    for row in outcome_epic_rows:
        if not row.get("compliant"):
            outcome_direct_issue_counts[row.get("story_key")] = outcome_direct_issue_counts.get(row.get("story_key"), 0) + 1

    def _field_issue_count(issue_key):
        return len(findings_by_entity.get(issue_key, []))

    def _child_metric_total(children, metric_key):
        return sum(child.get(metric_key, 0) for child in children)

    status_mismatch_count = sum(1 for row in cr_story_rows if not row.get("compliant"))
    status_mismatch_count += sum(1 for row in outcome_story_rows if not row.get("compliant"))
    status_mismatch_count += sum(1 for row in epic_rows if not row.get("compliant"))
    status_mismatch_count += sum(1 for row in outcome_epic_rows if not row.get("compliant"))
    broken_hierarchy_links = sum(1 for row in epic_rows if not row.get("compliant"))
    broken_hierarchy_links += sum(1 for row in outcome_epic_rows if not row.get("compliant"))

    response["kpis"].update({
        "status_mismatches": status_mismatch_count,
        "broken_hierarchy_links": broken_hierarchy_links,
    })

    hierarchy_started_at = time.perf_counter()
    labels_lookup = {}
    hierarchy_groups = {}
    outcome_path_by_story = {
        story_key: row.get("story_key")
        for story_key, row in story_outcome_by_story.items()
    }

    for outcome_key in sorted(outcome_keys):
        outcome_status = outcome_status_by_key.get(outcome_key)

        outcome_summary = issue_summaries.get(outcome_key, outcome_key)
        scrum_team = scrum_teams.get(outcome_key)
        outcome_labels = issue_labels.get(outcome_key, [])
        group_label = _outcome_group_label(outcome_labels, outcome_summary)
        labels_lookup[outcome_key] = group_label

        epic_nodes = []
        for epic_key in sorted(set(outcome_to_epics.get(outcome_key, []))):
            epic_status = epic_status_by_key.get(epic_key)

            cr_nodes = []
            for cr_key in sorted(set(epic_to_crs.get(epic_key, []))):
                story_nodes = []
                for story_row in sorted(story_rows_by_cr.get(cr_key, []), key=lambda item: item.get("story_key") or ""):
                    story_key = story_row.get("story_key")
                    story_status_issue_details = [
                        _build_status_detail(
                            story_key,
                            "Story",
                            row.get("story_status"),
                            row.get("cr_key"),
                            "CR",
                            row.get("cr_status"),
                            row,
                            "CR / Story",
                        )
                        for row in story_status_rows_by_story.get(story_key, [])
                    ]
                    story_field_issue_details = _field_issue_details(story_key)
                    status_mismatch_count = len(story_status_issue_details)
                    field_missing_count = len(story_field_issue_details)
                    issue_count = status_mismatch_count + field_missing_count
                    story_nodes.append({
                        "key": story_key,
                        "summary": issue_summaries.get(story_key, story_key),
                        "status": story_row.get("story_status"),
                        "type": "Story",
                        "assignee": assignee_by_key.get(story_key),
                        "statusMismatchCount": status_mismatch_count,
                        "fieldMissingCount": field_missing_count,
                        "issueCount": issue_count,
                        "complianceRate": _node_compliance_rate(issue_count, minimum_if_issues=60),
                        "statusIssueDetails": story_status_issue_details,
                        "fieldIssueDetails": story_field_issue_details,
                        "children": [],
                    })

                direct_cr_status_details = [
                    _build_status_detail(
                        cr_key,
                        "CR",
                        row.get("cr_status"),
                        row.get("story_key"),
                        "Story",
                        row.get("story_status"),
                        row,
                        "CR / Story",
                    )
                    for row in cr_status_rows_by_cr.get(cr_key, [])
                ]
                direct_cr_field_details = _field_issue_details(cr_key)
                total_cr_status_details = direct_cr_status_details + _status_issue_total(story_nodes, "statusIssueDetails")
                total_cr_field_details = direct_cr_field_details + _status_issue_total(story_nodes, "fieldIssueDetails")
                total_cr_status_issues = len(total_cr_status_details)
                total_cr_field_issues = len(total_cr_field_details)
                total_cr_issues = total_cr_status_issues + total_cr_field_issues
                cr_nodes.append({
                    "key": cr_key,
                    "summary": issue_summaries.get(cr_key, cr_key),
                    "status": cr_status_by_key.get(cr_key),
                    "type": "CR/Story",
                    "assignee": assignee_by_key.get(cr_key),
                    "statusMismatchCount": total_cr_status_issues,
                    "fieldMissingCount": total_cr_field_issues,
                    "issueCount": total_cr_issues,
                    "complianceRate": _node_compliance_rate(total_cr_issues),
                    "statusIssueDetails": total_cr_status_details,
                    "fieldIssueDetails": total_cr_field_details,
                    "children": story_nodes,
                })

            direct_epic_status_details = [
                _build_status_detail(
                    epic_key,
                    "Epic",
                    row.get("story_status"),
                    row.get("cr_key"),
                    "CR",
                    row.get("cr_status"),
                    row,
                    "Epic / CR",
                )
                for row in epic_status_rows_by_epic.get(epic_key, [])
            ]
            direct_epic_field_details = _field_issue_details(epic_key)
            total_epic_status_details = direct_epic_status_details + _status_issue_total(cr_nodes, "statusIssueDetails")
            total_epic_field_details = direct_epic_field_details + _status_issue_total(cr_nodes, "fieldIssueDetails")
            total_epic_status_issues = len(total_epic_status_details)
            total_epic_field_issues = len(total_epic_field_details)
            total_epic_issues = total_epic_status_issues + total_epic_field_issues
            epic_nodes.append({
                "key": epic_key,
                "summary": issue_summaries.get(epic_key, epic_key),
                "status": epic_status,
                "type": "Epic",
                "assignee": assignee_by_key.get(epic_key),
                "statusMismatchCount": total_epic_status_issues,
                "fieldMissingCount": total_epic_field_issues,
                "issueCount": total_epic_issues,
                "complianceRate": _node_compliance_rate(total_epic_issues),
                "statusIssueDetails": total_epic_status_details,
                "fieldIssueDetails": total_epic_field_details,
                "children": cr_nodes,
            })

        direct_outcome_status_details = []
        for row, child_type in outcome_status_rows_by_outcome.get(outcome_key, []):
            direct_outcome_status_details.append(
                _build_status_detail(
                    outcome_key,
                    "Outcome",
                    row.get("story_status"),
                    row.get("cr_key"),
                    "Epic" if child_type == "epic" else "Story",
                    row.get("cr_status"),
                    row,
                    "Outcome / Epic" if child_type == "epic" else "Outcome / Story",
                )
            )
        direct_outcome_field_details = _field_issue_details(outcome_key)
        total_outcome_status_details = direct_outcome_status_details + _status_issue_total(epic_nodes, "statusIssueDetails")
        total_outcome_field_details = direct_outcome_field_details + _status_issue_total(epic_nodes, "fieldIssueDetails")
        total_outcome_status_issues = len(total_outcome_status_details)
        total_outcome_field_issues = len(total_outcome_field_details)
        total_outcome_issues = total_outcome_status_issues + total_outcome_field_issues
        hierarchy_groups.setdefault(group_label, []).append({
            "key": outcome_key,
            "summary": outcome_summary,
            "status": outcome_status,
            "type": "Outcome",
            "assignee": assignee_by_key.get(outcome_key),
            "statusMismatchCount": total_outcome_status_issues,
            "fieldMissingCount": total_outcome_field_issues,
            "issueCount": total_outcome_issues,
            "complianceRate": _node_compliance_rate(total_outcome_issues),
            "statusIssueDetails": total_outcome_status_details,
            "fieldIssueDetails": total_outcome_field_details,
            "children": epic_nodes,
            "scrumTeam": scrum_team,
            "labels": sorted(outcome_labels),
        })

    ordered_hierarchy_labels = [label for label in OUTCOME_LABEL_ORDER if label in hierarchy_groups]
    ordered_hierarchy_labels.extend(
        sorted(label for label in hierarchy_groups.keys() if label not in OUTCOME_LABEL_ORDER)
    )

    response["hierarchy"] = [
        {
            "key": f"label:{label}",
            "label": label,
            "summary": f"{len(outcomes)} outcome{'s' if len(outcomes) != 1 else ''}",
            "type": "Label",
            "statusMismatchCount": sum(item.get("statusMismatchCount", 0) for item in outcomes),
            "fieldMissingCount": sum(item.get("fieldMissingCount", 0) for item in outcomes),
            "issueCount": sum(item.get("issueCount", 0) for item in outcomes),
            "complianceRate": _node_compliance_rate(sum(item.get("issueCount", 0) for item in outcomes)),
            "statusIssueDetails": [detail for item in outcomes for detail in item.get("statusIssueDetails", [])],
            "fieldIssueDetails": [detail for item in outcomes for detail in item.get("fieldIssueDetails", [])],
            "outcomes": sorted(outcomes, key=lambda item: item.get("key") or ""),
        }
        for label in ordered_hierarchy_labels
        for outcomes in [hierarchy_groups[label]]
    ]
    response["filters"]["labels"] = list(OUTCOME_LABEL_ORDER)
    app.logger.info(
        "[landing] Hierarchy assembly complete run_id=%s groups=%s duration_ms=%s",
        run_id,
        len(response["hierarchy"]),
        round((time.perf_counter() - hierarchy_started_at) * 1000, 2),
    )

    if not response["hierarchy"]:
        note = (
            "No hierarchy data could be derived for the current scan. Check the landing logs for pair counts, "
            "or run a fresh scan if Jira data has not been loaded yet."
        )
        response["data_notes"].append(note)
        app.logger.warning(
            "[landing] Hierarchy is empty after aggregation run_id=%s crs=%s epics=%s outcomes=%s issue_summaries=%s",
            run_id,
            len(cr_keys),
            len(epic_keys),
            len(outcome_keys),
            len(issue_summaries),
        )

    recent_issues = []
    for row in summary.get("active", []):
        story_key = row.get("story_key")
        outcome_key = outcome_path_by_story.get(story_key)
        cr_key = row.get("cr_key")
        epic_key = cr_to_epic.get(cr_key)
        path_parts = [labels_lookup.get(outcome_key), outcome_key, epic_key, cr_key, story_key]
        recent_issues.append({
            "key": story_key,
            "type": "Story",
            "summary": issue_summaries.get(story_key, story_key),
            "status": row.get("story_status"),
            "severity": row.get("severity"),
            "issueCategory": "Status mismatch",
            "reason": row.get("reason"),
            "hierarchyPath": " → ".join(part for part in path_parts if part),
            "owner": scrum_teams.get(story_key) or scrum_teams.get(cr_key) or "Delivery team",
            "nextAction": _next_action_for_issue("status_mismatch", row.get("severity"), row.get("reason")),
        })

    for finding in field_findings:
        issue_key = finding.get("entity_key")
        recent_issues.append({
            "key": issue_key,
            "type": str(finding.get("entity_type", "")).upper(),
            "summary": issue_summaries.get(issue_key, issue_key),
            "status": finding.get("entity_status"),
            "severity": finding.get("severity"),
            "issueCategory": "Mandatory field gap",
            "reason": finding.get("message"),
            "hierarchyPath": " → ".join(part for part in [scrum_teams.get(issue_key), issue_key] if part),
            "owner": scrum_teams.get(issue_key) or "Product team",
            "nextAction": _next_action_for_issue("field_gap", finding.get("severity"), finding.get("message")),
        })

    recent_issues.sort(key=lambda item: (-_issue_severity_rank(item.get("severity")), item.get("key") or ""))
    response["recent_issues"] = recent_issues[:24]

    issue_type_breakdown = {
        "Outcome": len({key for key in outcome_keys if key}),
        "Epic": len(epic_keys),
        "CR": len({row.get("cr_key") for row in cr_story_rows if row.get("cr_key")}),
        "Story": len({row.get("story_key") for row in cr_story_rows if row.get("story_key")}),
    }
    response["kpis"].update({
        "issue_type_breakdown": issue_type_breakdown,
        "total_outcomes": issue_type_breakdown["Outcome"],
        "total_epics": issue_type_breakdown["Epic"],
        "total_crs": issue_type_breakdown["CR"],
        "total_stories": issue_type_breakdown["Story"],
    })

    level_counts = {
        "Outcome": {
            "status": sum(1 for row in outcome_story_rows if not row.get("compliant")) + sum(1 for row in outcome_epic_rows if not row.get("compliant")),
            "fields": sum(1 for finding in field_findings if finding.get("entity_type") == "outcome"),
            "hierarchy": sum(1 for row in outcome_epic_rows if not row.get("compliant")),
            "pr": 0,
        },
        "Epic": {
            "status": sum(1 for row in epic_rows if not row.get("compliant")),
            "fields": sum(1 for finding in field_findings if finding.get("entity_type") == "epic"),
            "hierarchy": sum(1 for row in epic_rows if not row.get("compliant")),
            "pr": 0,
        },
        "CR": {
            "status": sum(1 for row in cr_story_rows if not row.get("compliant")),
            "fields": sum(1 for finding in field_findings if finding.get("entity_type") == "cr"),
            "hierarchy": sum(1 for row in cr_story_rows if not row.get("compliant")),
            "pr": 0,
        },
        "Story": {
            "status": sum(1 for row in cr_story_rows if not row.get("compliant")),
            "fields": 0,
            "hierarchy": 0,
            "pr": 0,
        },
    }
    response["compliance_matrix"] = [
        {
            "level": level,
            "status": counts["status"] == 0,
            "fields": counts["fields"] == 0,
            "hierarchy": counts["hierarchy"] == 0,
            "pr": counts["pr"] == 0,
            "overall": "Healthy" if sum(counts.values()) == 0 else ("Watch" if sum(counts.values()) < 4 else "Action"),
            "issueCount": sum(counts.values()),
        }
        for level, counts in level_counts.items()
    ]

    response["rule_metrics"] = [
        {
            "key": rule.get("key"),
            "label": rule.get("label"),
            "description": rule.get("description"),
            "severity": rule.get("severity"),
            "enabled": bool(rule.get("enabled", True)),
            "count": response["kpis"].get(rule.get("metric_key"), 0),
            "metric_key": rule.get("metric_key"),
        }
        for rule in config.get("rules", [])
    ]

    response["data_notes"].append(
        "Existing detailed dashboard remains unchanged and is available from the landing page quick actions."
    )
    app.logger.info(
        "[landing] Overview assembly timings run_id=%s duration_ms=%s",
        run_id,
        round((time.perf_counter() - build_started_at) * 1000, 2),
    )
    app.logger.info(
        "[landing] Overview ready run_id=%s hierarchy_groups=%s recent_issues=%s kpis=%s",
        run_id,
        len(response["hierarchy"]),
        len(response["recent_issues"]),
        {
            "overall": response["kpis"].get("overall_compliance"),
            "status": response["kpis"].get("status_mismatches"),
            "fields": response["kpis"].get("field_gaps"),
            "hierarchy": response["kpis"].get("broken_hierarchy_links"),
        },
    )
    return response


def _get_or_build_landing_overview(force_refresh=False):
    cache_lookup_started_at = time.perf_counter()
    config = load_landing_config()
    summary = compliance_db.get_latest_summary()
    cache_key = _landing_cache_key(summary, config)

    if not force_refresh:
        cached = _get_cached_landing_overview(cache_key)
        if cached is not None:
            app.logger.info(
                "[landing] Cache hit run_id=%s duration_ms=%s",
                summary.get("run_id"),
                round((time.perf_counter() - cache_lookup_started_at) * 1000, 2),
            )
            return cached

    app.logger.info(
        "[landing] Cache miss run_id=%s force_refresh=%s",
        summary.get("run_id"),
        force_refresh,
    )
    overview = _build_landing_overview(summary=summary, config=config)
    _store_cached_landing_overview(cache_key, overview)
    return overview


@app.route("/api/entities/<entity_type>/<entity_key>/fields", methods=["GET"])
def get_entity_current_fields(entity_type, entity_key):
    """Returns EVERY field for this entity type with its current value —
    not just the ones flagged as missing/invalid — so the dashboard can
    show every field as editable, pre-filled where a value already exists."""
    if entity_type not in _FIELD_MAPS:
        return jsonify({"error": "entity_type must be cr, epic, or outcome"}), 400
    _, labels, types = _FIELD_MAPS[entity_type]
    field_ids = _effective_field_map(entity_type)

    try:
        raw = jira_rest.get_issue_raw(entity_key)
    except Exception as e:
        return jsonify({"error": f"Could not fetch issue: {e}"}), 500

    fields = raw.get("fields", {})
    result = []
    for key, field_id in field_ids.items():
        value = field_rules._value(fields, field_id)
        result.append({
            "key": key, "field_id": field_id, "label": labels.get(key, key),
            "type": types.get(key, "text"), "value": value,
            "options": field_rules.SELECT_OPTIONS.get(key),
        })
    return jsonify({"entity_key": entity_key, "fields": result})


@app.route("/api/configuration/compliance", methods=["GET", "POST"])
def landing_configuration():
    if request.method == "GET":
        return jsonify(load_landing_config())

    payload = request.get_json(silent=True) or {}
    saved = save_landing_config(payload.get("config", payload))
    _invalidate_landing_overview_cache(reason="configuration-updated")
    return jsonify(saved)


@app.route("/api/landing/overview", methods=["GET"])
def landing_overview():
    app.logger.info("[landing] /api/landing/overview requested")
    try:
        overview = _get_or_build_landing_overview()
        app.logger.info(
            "[landing] /api/landing/overview response hierarchy_groups=%s total_outcomes=%s total_crs=%s",
            len(overview.get("hierarchy", [])),
            overview.get("kpis", {}).get("total_outcomes", 0),
            overview.get("kpis", {}).get("total_crs", 0),
        )
        return jsonify(overview)
    except Exception as e:
        app.logger.exception("[landing] Failed to build landing overview: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/jira/transitions/<issue_key>", methods=["GET"])
def get_transitions(issue_key):
    try:
        transitions = jira_rest.get_available_transitions(issue_key)
    except Exception as e:
        return jsonify({"error": f"Could not fetch transitions: {e}"}), 500
    return jsonify({"issue_key": issue_key, "transitions": transitions})


@app.route("/api/jira/transition", methods=["POST"])
def apply_transition():
    body = request.get_json(silent=True) or {}
    issue_key = body.get("issue_key")
    transition_id = body.get("transition_id")
    if not issue_key or not transition_id:
        return jsonify({"error": "Provide issue_key and transition_id"}), 400
    try:
        result = jira_rest.transition_issue(issue_key, transition_id)
    except Exception as e:
        return jsonify({"error": f"Transition failed: {e}"}), 500

    # A status change can cascade across multiple stored relationships
    # (this issue as a Story vs its CR, or as a CR feeding an Epic's
    # bottleneck check, etc.) — rather than trying to patch every
    # possibly-affected row precisely, trigger a full rescan in the
    # background so the dashboard is correct again within moments,
    # without blocking this response on a scan that could take a while
    # at scale.
    started = _start_background_scan(source=f"transition:{issue_key}")
    if started.get("already_running"):
        app.logger.info("[transition] Scan already running; skipped duplicate trigger for %s", issue_key)

    return jsonify({"transitioned": True, "issue_key": issue_key, **result})


@app.route("/api/jira/update-field", methods=["POST"])
def update_field():
    body = request.get_json(silent=True) or {}
    issue_key = body.get("issue_key")
    entity_type = body.get("entity_type")
    field_key = body.get("field_key")
    value = body.get("value")
    if not all([issue_key, entity_type, field_key]):
        return jsonify({"error": "Provide issue_key, entity_type, field_key"}), 400
    if entity_type not in _FIELD_MAPS:
        return jsonify({"error": "entity_type must be cr, epic, or outcome"}), 400

    _, labels, types = _FIELD_MAPS[entity_type]
    field_ids = _effective_field_map(entity_type)
    if field_key not in field_ids:
        return jsonify({"error": f"Unknown field_key '{field_key}' for {entity_type}"}), 400

    field_id = field_ids[field_key]
    field_type = types.get(field_key, "text")
    shaped_value = field_rules.format_field_value(field_key, field_type, value)

    try:
        result = jira_rest.update_issue_fields(issue_key, {field_id: shaped_value})
    except Exception as e:
        return jsonify({"error": f"Update failed: {e}. If this is a select field, "
                                  f"the value/options here may not match Jira's real "
                                  f"options — check Project Settings → Fields."}), 500

    # Re-check this one entity immediately and patch the stored findings —
    # without this, the dashboard keeps showing the OLD (pre-edit) result
    # until the next full scan, since it reads stored scan results, not
    # live Jira data. Best-effort: if the re-check fails, the write to
    # Jira already succeeded, so we still report success — the next
    # scheduled scan will catch up regardless.
    try:
        run_id = compliance_db.latest_field_run_id()
        if run_id:
            raw = jira_rest.get_issue_raw(issue_key)
            fresh_status = (raw.get("fields", {}).get("status") or {}).get("name")
            check_func = _CHECK_FUNCS[entity_type]
            fresh_findings = check_func(raw.get("fields", {}), fresh_status, field_map=_effective_field_map(entity_type))
            compliance_db.replace_field_findings_for_entity(run_id, entity_type, issue_key, fresh_status, fresh_findings)
    except Exception as e:
        print(f"[update_field] wrote to Jira OK but immediate re-check failed for {issue_key}: {e}")

    _invalidate_landing_overview_cache(reason=f"field-updated:{issue_key}")

    return jsonify({"updated": True, "issue_key": issue_key, "field_key": field_key, **result})


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


@app.route("/api/dashboard/db-health", methods=["GET"])
def db_health():
    size_bytes = compliance_db.get_db_size_bytes()
    return jsonify({
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "retention_days": int(os.environ.get("RETENTION_DAYS", 7)),
    })


@app.route("/api/dashboard/prune", methods=["POST"])
def manual_prune():
    days = int(request.args.get("days", os.environ.get("RETENTION_DAYS", 7)))
    result = compliance_db.prune_old_runs(keep_days=days)
    compliance_db.vacuum()
    return jsonify(result)


@app.route("/api/dashboard/refresh", methods=["POST"])
def dashboard_refresh():
    if not _has_runtime_jira_credentials():
        app.logger.warning("[jira-runtime] Refresh requested without runtime Jira credentials")
        return jsonify({"error": _runtime_jira_error_message()}), 401
    body = request.get_json(silent=True) or {}
    project_key = (body.get("project_key") or "").strip() or None
    board_id = body.get("board_id") or None
    background = bool(body.get("background") or body.get("async"))

    if background:
        state = _start_background_scan(project_key=project_key, board_id=board_id, source="dashboard_refresh")
        return jsonify(state), 202

    result = run_async(_run_full_scan(project_key=project_key, board_id=board_id))
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/dashboard/refresh-status", methods=["GET"])
def dashboard_refresh_status():
    return jsonify(_scan_state_snapshot())


@app.route("/api/config", methods=["GET"])
def get_frontend_config():
    if not _has_runtime_jira_credentials() and "jira_config" in session:
        app.logger.warning("[jira-runtime] Frontend config requested with stale Jira session but no runtime credentials")
    jira_url = os.environ.get("JIRA_URL", "").rstrip("/")
    # Default to 'ACC' project if DEFAULT_PROJECT_KEY is not explicitly set
    default_project = os.environ.get("DEFAULT_PROJECT_KEY", "ACC")
    app.logger.info("Frontend config: jira_url=%s default_project_key=%s", jira_url, default_project)
    return jsonify({
        "jira_base": f"{jira_url}/browse/" if jira_url else "",
        "default_project_key": default_project,
    })


@app.route("/api/jira/projects", methods=["GET"])
def list_projects():
    if not _has_runtime_jira_credentials():
        app.logger.warning("[jira-runtime] Project list requested without runtime Jira credentials")
        return jsonify({"error": _runtime_jira_error_message()}), 401
    try:
        projects = jira_rest.get_projects()
    except Exception as e:
        return jsonify({"error": f"Could not fetch projects: {e}"}), 500
    return jsonify({"items": projects, "default_project_key": os.environ.get("DEFAULT_PROJECT_KEY", "")})


@app.route("/api/jira/boards", methods=["GET"])
def list_boards():
    if not _has_runtime_jira_credentials():
        app.logger.warning("[jira-runtime] Board list requested without runtime Jira credentials")
        return jsonify({"error": _runtime_jira_error_message()}), 401
    project_key = request.args.get("project_key") or None
    try:
        boards = jira_rest.get_boards(project_key=project_key)
    except Exception as e:
        return jsonify({"error": f"Could not fetch boards: {e}"}), 500
    # Dropdown only ever offers the same boards the scan is restricted to
    # (ALLOWED_BOARD_NAMES) — never a choice that would return nothing.
    #boards = [b for b in boards if b.get("name") in ALLOWED_BOARD_NAMES]
    return jsonify({"items": boards})


@app.route("/api/dashboard/summary", methods=["GET"])
def dashboard_summary():
    return jsonify(compliance_db.get_latest_summary())


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
    _invalidate_landing_overview_cache(reason=f"resolve:{story_key}:{cr_key}")
    return jsonify({"resolved": True, "story_key": story_key, "cr_key": cr_key})


@app.route("/api/dashboard/unresolve", methods=["POST"])
def dashboard_unresolve():
    body = request.get_json(silent=True) or {}
    story_key, cr_key = body.get("story_key"), body.get("cr_key")
    if not story_key or not cr_key:
        return jsonify({"error": "Provide story_key and cr_key"}), 400
    compliance_db.unresolve(story_key, cr_key)
    _invalidate_landing_overview_cache(reason=f"unresolve:{story_key}:{cr_key}")
    return jsonify({"resolved": False, "story_key": story_key, "cr_key": cr_key})


# ---------------------------------------------------------------------------
# Scheduler: re-run the full scan on an interval so the dashboard reflects
# near-real-time state without the user manually clicking refresh.
# ---------------------------------------------------------------------------
def start_scheduler1():
    from apscheduler.schedulers.background import BackgroundScheduler
    interval_minutes = int(os.environ.get("SCAN_INTERVAL_MINUTES", 15))
    retention_days = int(os.environ.get("RETENTION_DAYS", 7))

    def scan_and_prune1():
        run_async(_run_full_scan())
        pruned = compliance_db.prune_old_runs(keep_days=retention_days)
        if pruned["pruned_runs"] > 0:
            _log_scan_progress("[retention] pruned %s runs older than %s days", pruned["pruned_runs"], retention_days)

    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_and_prune, "interval", minutes=interval_minutes)
    scheduler.add_job(compliance_db.vacuum, "interval", hours=24)
    scheduler.start()
    # Run once immediately on startup so the dashboard has data right away
    scan_and_prune()
    return scheduler

def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    interval_minutes = int(os.environ.get("SCAN_INTERVAL_MINUTES", 15))
    retention_days = int(os.environ.get("RETENTION_DAYS", 7))

    def scan_and_prune():
        try:
            _log_scan_progress("[scheduler] Starting scheduled scan...")
            run_async(_run_full_scan())

            pruned = compliance_db.prune_old_runs(
                keep_days=retention_days
            )

            if pruned["pruned_runs"] > 0:
                _log_scan_progress("[retention] pruned %s runs older than %s days", pruned["pruned_runs"], retention_days)

        except Exception as e:
            app.logger.exception(
                "[scheduler] Scan failed: %s", e
            )

    scheduler = BackgroundScheduler()

    scheduler.add_job(
        scan_and_prune,
        "interval",
        minutes=interval_minutes,
        id="compliance_scan",
        replace_existing=True,
    )

    scheduler.add_job(
        compliance_db.vacuum,
        "interval",
        hours=24,
        id="database_vacuum",
        replace_existing=True,
    )

    scheduler.start()

    # IMPORTANT:
    # Do NOT run scan_and_prune() here.
    # The first scan will happen after SCAN_INTERVAL_MINUTES.
    _log_scan_progress("[scheduler] Started. First scan in %s minutes.", interval_minutes)

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
    mode = body.get("mode", "phase")

    if mode == "fields":
        # Field-completeness comment: entity + list of missing/invalid fields.
        # Frontend already has this data (from /api/fields/findings), so it's
        # passed directly rather than re-queried here.
        entity_key = body.get("entity_key")
        entity_status = body.get("entity_status")
        findings = body.get("findings") or []
        if not entity_key or not findings:
            return jsonify({"error": "Provide entity_key and findings"}), 400
        try:
            draft = ai_draft.draft_field_comment(entity_key, entity_status, findings)
        except Exception as e:
            return jsonify({"error": f"Local LLM call failed: {e}. Is Ollama running?"}), 500
        return jsonify({"issue_key": entity_key, "draft": draft})

    # Phase-mismatch comment: any two-entity comparison (Story/CR, Epic/CR,
    # Story/Outcome). Frontend passes the specific row's data directly —
    # avoids needing pair-type-aware lookup logic here.
    target_key = body.get("target_key")
    target_status = body.get("target_status")
    other_key = body.get("other_key")
    other_status = body.get("other_status")
    reason = body.get("reason")
    severity = body.get("severity")
    if not target_key or not other_key:
        return jsonify({"error": "Provide target_key and other_key"}), 400

    # Optionally enrich with the target's description for a more grounded draft
    target_desc = ""
    desc_result = run_async(_call_mcp_tool("jira_get_issue", {"issue_key": target_key}))
    if isinstance(desc_result, list):
        import json as _json
        for block in desc_result:
            text = block.get("text")
            if not text:
                continue
            try:
                parsed = _json.loads(text)
                target_desc = parsed.get("description", "")
                break
            except Exception:
                continue

    try:
        draft = ai_draft.draft_remediation_comment(
            target_key=target_key, target_status=target_status, target_desc=target_desc,
            other_key=other_key, other_status=other_status, reason=reason, severity=severity,
        )
    except Exception as e:
        return jsonify({"error": f"Local LLM call failed: {e}. Is Ollama running?"}), 500

    return jsonify({"issue_key": target_key, "draft": draft})


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


# ============================================================================
# Jira Login Routes with LangGraph and RAG
# ============================================================================
@app.route("/api/jira/validate-url", methods=["POST"])
def validate_jira_url():
    """Validate Jira URL connectivity"""
    body = request.get_json(silent=True) or {}
    jira_url = body.get("jira_url", "").strip()

    if not jira_url:
        return jsonify({"error": "Provide jira_url"}), 400

    try:
        app.logger.info("[jira-login] validate_jira_url called for url=%s", jira_url)
        base_url = jira_url.rstrip('/')

        # First try Data Center / Server style endpoint.
        result = requests.get(f"{base_url}/rest/api/2/serverInfo", timeout=5)
        app.logger.info(
            "[jira-login] validate_jira_url serverInfo status=%s url=%s",
            result.status_code,
            jira_url,
        )

        if result.status_code == 200:
            data = result.json()
            app.logger.info(
                "[jira-login] validate_jira_url success version=%s deployment_type=%s url=%s",
                data.get("version"),
                data.get("deploymentType", "server"),
                jira_url,
            )
            return jsonify({
                "valid": True,
                "version": data.get("version"),
                "deployment_type": data.get("deploymentType", "server"),
                "message": f"✓ Valid Jira instance: v{data.get('version')}"
            })

        # Cloud often returns 404 on /serverInfo, so fall back to a cloud endpoint.
        cloud_result = requests.get(f"{base_url}/rest/api/3/myself", timeout=5)
        app.logger.info(
            "[jira-login] validate_jira_url cloud fallback status=%s url=%s",
            cloud_result.status_code,
            jira_url,
        )

        if cloud_result.status_code in (200, 401, 403):
            app.logger.info(
                "[jira-login] validate_jira_url detected cloud via fallback url=%s status=%s",
                jira_url,
                cloud_result.status_code,
            )
            return jsonify({
                "valid": True,
                "version": None,
                "deployment_type": "cloud",
                "message": "✓ Valid Jira Cloud instance"
            })

        app.logger.warning(
            "[jira-login] validate_jira_url failed serverInfo_status=%s cloud_status=%s url=%s serverInfo_body=%s cloud_body=%s",
            result.status_code,
            cloud_result.status_code,
            jira_url,
            result.text[:500],
            cloud_result.text[:500],
        )
        return jsonify({
            "valid": False,
            "error": f"HTTP {result.status_code} (serverInfo), HTTP {cloud_result.status_code} (cloud fallback)",
            "message": f"Could not connect: serverInfo={result.status_code}, cloud={cloud_result.status_code}"
        })
    except Exception as e:
        app.logger.exception(
            "[jira-login] validate_jira_url exception url=%s error=%s",
            jira_url,
            e,
        )
        return jsonify({
            "valid": False,
            "error": str(e),
            "message": f"Could not connect: {str(e)}"
        }), 500


@app.route("/api/jira/guidance", methods=["POST"])
def get_jira_guidance_route():
    """Get RAG-based Jira setup guidance"""
    body = request.get_json(silent=True) or {}
    jira_type = body.get("jira_type", "")
    question = body.get("question", "How to setup?")

    try:
        guidance = get_jira_guidance(question, jira_type)
        return jsonify({
            "guidance": guidance,
            "jira_type": jira_type
        })
    except Exception as e:
        return jsonify({
            "error": f"Could not retrieve guidance: {str(e)}",
            "guidance": ""
        })


@app.route("/api/jira/login", methods=["POST"])
def jira_agentic_login():
    """Main endpoint for agentic Jira login with LangGraph"""
    body = request.get_json(silent=True) or {}

    jira_url = body.get("jira_url", "").strip()
    username = body.get("username", "").strip()
    password_or_token = body.get("password_or_token", "").strip()
    jira_type = body.get("jira_type", "").strip()

    # Validate required fields
    if not all([jira_url, username, password_or_token]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        # Run agentic login workflow
        result = handle_jira_login(
            jira_url=jira_url,
            username=username,
            password_or_token=password_or_token,
            jira_type=jira_type
        )

        if result["success"]:
            # Store non-sensitive profile data separately from credentials.
            session["jira_config"] = {
                "url": result["jira_url"],
                "username": result["username"],
                "type": result["jira_type"],
                "authenticated": True
            }
            session["jira_auth"] = {
                "url": result["jira_url"],
                "username": result["username"],
                "password": password_or_token,
            }

            # Keep environment variables in sync for background scans and
            # any work that runs outside an active request/session context.
            os.environ["JIRA_URL"] = result["jira_url"]
            os.environ["JIRA_USERNAME"] = result["username"]
            os.environ["JIRA_PASSWORD"] = password_or_token

            app.logger.info(f"Jira login successful for {username} on {result['jira_type']}")
            return jsonify(result), 200
        else:
            app.logger.warning(f"Jira login failed for {username}: {result.get('error')}")
            return jsonify(result), 401

    except Exception as e:
        app.logger.exception("Jira login error: %s", e)
        return jsonify({
            "success": False,
            "error": str(e),
            "messages": ["❌ Error during login workflow"]
        }), 500


@app.route("/api/jira/test-connection", methods=["POST"])
def jira_test_connection():
    """Test Jira URL + credentials without creating a session."""
    body = request.get_json(silent=True) or {}

    jira_url = body.get("jira_url", "").strip()
    username = body.get("username", "").strip()
    password_or_token = body.get("password_or_token", "").strip()
    jira_type = body.get("jira_type", "").strip()

    if not all([jira_url, username, password_or_token]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        app.logger.info(
            "[jira-login] test_connection called url=%s user=%s type=%s",
            jira_url,
            username,
            jira_type or "auto",
        )

        result = handle_jira_login(
            jira_url=jira_url,
            username=username,
            password_or_token=password_or_token,
            jira_type=jira_type,
        )

        status = 200 if result.get("success") else 401
        if result.get("success"):
            app.logger.info(
                "[jira-login] test_connection success url=%s user=%s type=%s",
                jira_url,
                username,
                result.get("jira_type", jira_type),
            )
        else:
            app.logger.warning(
                "[jira-login] test_connection failed url=%s user=%s type=%s error=%s",
                jira_url,
                username,
                result.get("jira_type", jira_type),
                result.get("error"),
            )

        return jsonify({
            "success": result.get("success", False),
            "jira_type": result.get("jira_type", jira_type),
            "jira_url": result.get("jira_url", jira_url),
            "username": result.get("username", username),
            "messages": result.get("messages", []),
            "error": result.get("error", ""),
            "validation_details": result.get("validation_details", {}),
        }), status

    except Exception as e:
        app.logger.exception("[jira-login] test_connection error: %s", e)
        return jsonify({
            "success": False,
            "error": str(e),
            "messages": ["❌ Error during connection test"],
        }), 500


@app.route("/api/jira/check-session", methods=["GET"])
def check_jira_session():
    """Check if Jira credentials are configured"""
    has_session = "jira_config" in session
    has_runtime_creds = _has_runtime_jira_credentials()

    if has_session and "jira_auth" not in session:
        app.logger.warning("[jira-runtime] Clearing stale Jira session because session credentials are missing")
        session.pop("jira_config", None)
        session.pop("jira_auth", None)
        has_session = False

    return jsonify({
        "authenticated": has_session,
        "config": session.get("jira_config", {}),
        "runtime_ready": has_runtime_creds,
    })


@app.route("/api/jira/logout", methods=["POST"])
def jira_logout():
    """Clear Jira session"""
    if "jira_config" in session:
        del session["jira_config"]
    session.pop("jira_auth", None)
    os.environ.pop("JIRA_URL", None)
    os.environ.pop("JIRA_USERNAME", None)
    os.environ.pop("JIRA_PASSWORD", None)
    app.logger.info("Jira session cleared")
    return jsonify({"success": True})


# ============================================================================
# HIERARCHY TRACER & COMPLIANCE API
# ============================================================================

@app.route("/api/hierarchy/summary", methods=["GET"])
def hierarchy_summary():
    """Quick summary: count of issues by label and overall hierarchy stats."""
    if not _has_runtime_jira_credentials():
        return jsonify({"error": _runtime_jira_error_message()}), 401

    async def run_trace():
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tracer = HierarchyTracer(
                    session, jira_rest, compliance_rules, field_rules, app.logger
                )
                return await tracer.get_hierarchy_summary()

    result = run_async(run_trace())
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/hierarchy/epic-focus", methods=["GET"])
def hierarchy_epic_focus():
    """Return exact JQL plus an epic-scoped hierarchy slice for dashboard focus mode."""
    if not _has_runtime_jira_credentials():
        return jsonify({"error": _runtime_jira_error_message()}), 401

    epic_key = (request.args.get("epic_key") or "").strip().upper()
    if not epic_key:
        return jsonify({"error": "Provide ?epic_key=EPIC-123"}), 400

    def _nested_name(value):
        if isinstance(value, dict):
            return value.get("name") or value.get("displayName") or value.get("value") or value.get("key")
        return value

    def _issue_value(issue, field_name, nested_name=False):
        value = issue.get(field_name)
        if value is None:
            fields = issue.get("fields") or {}
            value = fields.get(field_name)
        return _nested_name(value) if nested_name else value

    def _issue_type_name(issue):
        value = issue.get("issue_type")
        if value is None:
            value = (issue.get("fields") or {}).get("issuetype")
        return _nested_name(value) or "Issue"

    def _issue_status_name(issue):
        value = issue.get("status")
        if value is None:
            value = (issue.get("fields") or {}).get("status")
        return _nested_name(value) or "Unknown"

    def _issue_assignee_name(issue):
        value = issue.get("assignee")
        if value is None:
            value = (issue.get("fields") or {}).get("assignee")
        return _nested_name(value)

    def _issue_parent_key(issue):
        parent = issue.get("parent")
        if parent is None:
            parent = (issue.get("fields") or {}).get("parent")
        if isinstance(parent, dict):
            return parent.get("key")
        return None

    def _shape_issue(issue, *, relation=None):
        key = issue.get("key")
        issue_type = _issue_type_name(issue)
        return {
            "key": key,
            "summary": issue.get("summary") or ((issue.get("fields") or {}).get("summary")) or key,
            "status": _issue_status_name(issue),
            "issue_type": issue_type,
            "assignee": _issue_assignee_name(issue),
            "parent_key": _issue_parent_key(issue),
            "relation": relation,
            "is_cr": issue_type.strip().lower() in {"change request", "cr"},
        }

    pair_type_meta = {
        "cr_story": {"cr_label": "CR", "story_label": "Story", "rule_label": "CR → Story"},
        "epic_cr": {"cr_label": "CR", "story_label": "Epic", "rule_label": "CR → Epic"},
        "epic_story": {"cr_label": "Epic", "story_label": "Story", "rule_label": "Epic → Story"},
        "story_outcome": {"cr_label": "Story", "story_label": "Outcome", "rule_label": "Story → Outcome"},
        "outcome_epic": {"cr_label": "Epic", "story_label": "Outcome", "rule_label": "Outcome → Epic"},
    }

    async def run_focus():
        started_at = time.perf_counter()
        epic_raw_task = asyncio.to_thread(_safe_get_issue_raw, epic_key)
        latest_run_id = compliance_db.latest_run_id()

        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                exact_jql = f'(parent = "{epic_key}" OR "Epic Link" = "{epic_key}") ORDER BY key'
                cr_only_jql = f'filter = Asylum_CRs AND (parent = "{epic_key}" OR "Epic Link" = "{epic_key}") ORDER BY key'

                try:
                    direct_issues = await _search_all_issues(session, exact_jql)
                except JiraSearchError as search_error:
                    _log_scan_progress(
                        "[epic-focus] combined exact JQL failed for %s, retrying narrower queries: %s",
                        epic_key,
                        search_error,
                        level="warning",
                    )
                    fallback_issue_by_key = {}
                    fallback_errors = []
                    for fallback_jql in (
                        f'parent = "{epic_key}" ORDER BY key',
                        f'"Epic Link" = "{epic_key}" ORDER BY key',
                    ):
                        try:
                            for issue in await _search_all_issues(session, fallback_jql):
                                issue_key = issue.get("key")
                                if issue_key:
                                    fallback_issue_by_key[issue_key] = issue
                        except JiraSearchError as fallback_error:
                            fallback_errors.append(str(fallback_error))

                    if fallback_issue_by_key:
                        direct_issues = [fallback_issue_by_key[key] for key in sorted(fallback_issue_by_key)]
                    else:
                        joined_errors = "; ".join(fallback_errors) if fallback_errors else str(search_error)
                        return {"error": f"Jira could not build the 'issues in epic' view for {epic_key}: {joined_errors}"}

                try:
                    cr_candidates = await _search_all_issues(session, cr_only_jql)
                except Exception as e:
                    _log_scan_progress(
                        "[epic-focus] could not evaluate CR-only JQL for %s: %s",
                        epic_key,
                        e,
                        level="warning",
                    )
                    cr_candidates = []

                direct_issue_by_key = {
                    issue.get("key"): issue
                    for issue in direct_issues
                    if issue.get("key")
                }
                cr_issue_by_key = {
                    issue.get("key"): issue
                    for issue in cr_candidates
                    if issue.get("key")
                }

                for issue in direct_issues:
                    shaped = _shape_issue(issue)
                    if shaped["is_cr"] and shaped["key"] not in cr_issue_by_key:
                        cr_issue_by_key[shaped["key"]] = issue

                linked_by_cr = await _linked_issues_concurrent(session, sorted(cr_issue_by_key)) if cr_issue_by_key else {}

        epic_raw = await epic_raw_task
        if not epic_raw:
            return {"error": f"Could not fetch Jira issue {epic_key}. Confirm the epic key exists and your session has access."}

        epic_fields = epic_raw.get("fields", {})
        outcome_key = None
        parent = epic_fields.get("parent")
        if isinstance(parent, dict):
            outcome_key = parent.get("key")
        if not outcome_key:
            outcome_key = epic_fields.get(PARENT_LINK_FIELD_ID)

        outcome_raw = await asyncio.to_thread(_safe_get_issue_raw, outcome_key) if outcome_key else None
        outcome_fields = (outcome_raw or {}).get("fields", {})
        epic_status_name = _nested_name(epic_fields.get("status")) or "Unknown"
        outcome_status_name = _nested_name(outcome_fields.get("status")) or "Unknown"

        issue_summaries = compliance_db.get_issue_summaries()
        epic_cr_relationships = compliance_db.get_issue_relationships(
            relationship_type="epic_contains_cr",
            parent_keys=[epic_key],
        )

        field_findings_by_key = defaultdict(list)
        for finding in compliance_db.get_latest_field_findings():
            entity_key = finding.get("entity_key")
            if not entity_key:
                continue
            field_findings_by_key[entity_key].append({
                "field": finding.get("field"),
                "severity": finding.get("severity"),
                "message": finding.get("message"),
                "status": finding.get("entity_status"),
            })

        pair_rows_by_key = defaultdict(list)
        all_pair_rows = []
        if latest_run_id:
            all_pair_rows.extend(compliance_db.get_pair_findings(latest_run_id, "cr_story", include_compliant=True))
            all_pair_rows.extend(compliance_db.get_pair_findings(latest_run_id, "epic_cr", include_compliant=True))
            all_pair_rows.extend(compliance_db.get_pair_findings(latest_run_id, "story_outcome", include_compliant=True))
            all_pair_rows.extend(compliance_db.get_pair_findings(latest_run_id, "outcome_epic", include_compliant=True))

        epic_relationship_cr_keys = {
            rel.get("child_key")
            for rel in epic_cr_relationships
            if rel.get("child_key")
        }
        epic_pair_cr_keys = {
            row.get("cr_key")
            for row in all_pair_rows
            if row.get("pair_type") == "epic_cr" and row.get("story_key") == epic_key and row.get("cr_key")
        }
        epic_cr_keys = set(epic_relationship_cr_keys) | set(epic_pair_cr_keys)

        cr_story_rows_by_cr = defaultdict(list)
        for row in all_pair_rows:
            if row.get("pair_type") == "cr_story" and row.get("cr_key"):
                cr_story_rows_by_cr[row.get("cr_key")].append(row)

        epic_story_keys = {
            row.get("story_key")
            for cr_key in epic_cr_keys
            for row in cr_story_rows_by_cr.get(cr_key, [])
            if row.get("story_key")
        }

        def _synthetic_issue(issue_key, issue_type, status=None):
            return {
                "key": issue_key,
                "summary": issue_summaries.get(issue_key, issue_key),
                "status": {"name": status} if status else None,
                "issue_type": {"name": issue_type},
                "fields": {
                    "summary": issue_summaries.get(issue_key, issue_key),
                    "status": {"name": status} if status else None,
                    "issuetype": {"name": issue_type},
                },
            }

        supplemental_status_hints = {}
        for row in all_pair_rows:
            if row.get("cr_key"):
                supplemental_status_hints.setdefault(row.get("cr_key"), row.get("cr_status"))
            if row.get("story_key"):
                supplemental_status_hints.setdefault(row.get("story_key"), row.get("story_status"))

        supplemental_type_hints = {key: "Change Request" for key in epic_cr_keys}
        supplemental_type_hints.update({key: "Story" for key in epic_story_keys})

        supplemental_issue_keys = (epic_cr_keys | epic_story_keys) - set(direct_issue_by_key.keys())
        supplemental_raw_issues = await _fetch_raw_concurrent(list(supplemental_issue_keys)) if supplemental_issue_keys else {}

        for issue_key in supplemental_issue_keys:
            raw_issue = supplemental_raw_issues.get(issue_key)
            if raw_issue:
                direct_issue_by_key[issue_key] = raw_issue
                continue

            issue_type = supplemental_type_hints.get(issue_key, "Issue")
            issue_status = supplemental_status_hints.get(issue_key)
            direct_issue_by_key[issue_key] = _synthetic_issue(issue_key, issue_type, issue_status)

        pair_row_keys = {
            (row.get("pair_type"), row.get("cr_key"), row.get("story_key"))
            for row in all_pair_rows
            if row.get("pair_type") and row.get("cr_key") and row.get("story_key")
        }

        def _append_live_pair_row(pair_type, *, cr_key, cr_status, story_key, story_status, verdict, source="live_hierarchy"):
            if not pair_type or not cr_key or not story_key or not verdict:
                return
            row_key = (pair_type, cr_key, story_key)
            if row_key in pair_row_keys:
                return
            pair_row_keys.add(row_key)
            all_pair_rows.append({
                "pair_type": pair_type,
                "cr_key": cr_key,
                "cr_status": cr_status,
                "story_key": story_key,
                "story_status": story_status,
                "compliant": bool(verdict.get("compliant")),
                "reason": verdict.get("reason"),
                "severity": verdict.get("severity"),
                "score": verdict.get("score"),
                "source": source,
            })

        def _issue_type_slug(issue):
            issue_type = (_issue_type_name(issue) or "Issue").strip().lower()
            if issue_type in {"change request", "cr"}:
                return "cr"
            if issue_type == "epic":
                return "epic"
            if issue_type == "outcome":
                return "outcome"
            return "story"

        if outcome_key:
            _append_live_pair_row(
                "outcome_epic",
                cr_key=epic_key,
                cr_status=epic_status_name,
                story_key=outcome_key,
                story_status=outcome_status_name,
                verdict=compliance_rules.evaluate_pair(
                    outcome_status_name,
                    "outcome",
                    epic_status_name,
                    "epic",
                    driver_label="Outcome",
                    dependent_label="Epic",
                ),
            )

        story_issue_keys = {
            issue_key
            for issue_key, issue in direct_issue_by_key.items()
            if issue_key and _issue_type_slug(issue) == "story"
        }

        for story_key in sorted(story_issue_keys):
            story_status_name = _issue_status_name(direct_issue_by_key.get(story_key))
            _append_live_pair_row(
                "epic_story",
                cr_key=epic_key,
                cr_status=epic_status_name,
                story_key=story_key,
                story_status=story_status_name,
                verdict=compliance_rules.evaluate_pair(
                    epic_status_name,
                    "epic",
                    story_status_name,
                    "story",
                    driver_label="Epic",
                    dependent_label="Story",
                ),
            )
            if outcome_key:
                _append_live_pair_row(
                    "story_outcome",
                    cr_key=story_key,
                    cr_status=story_status_name,
                    story_key=outcome_key,
                    story_status=outcome_status_name,
                    verdict=compliance_rules.evaluate_outcome(story_status_name, outcome_status_name),
                )

        for cr_key in sorted(set(cr_issue_by_key) | epic_cr_keys):
            cr_issue = direct_issue_by_key.get(cr_key) or cr_issue_by_key.get(cr_key) or _synthetic_issue(cr_key, "Change Request", supplemental_status_hints.get(cr_key))
            cr_status_name = _issue_status_name(cr_issue)
            _append_live_pair_row(
                "epic_cr",
                cr_key=cr_key,
                cr_status=cr_status_name,
                story_key=epic_key,
                story_status=epic_status_name,
                verdict=compliance_rules.evaluate_pair(
                    cr_status_name,
                    "cr",
                    epic_status_name,
                    "epic",
                    driver_label="CR",
                    dependent_label="Epic",
                ),
            )

            child_story_keys = {
                row.get("story_key")
                for row in cr_story_rows_by_cr.get(cr_key, [])
                if row.get("story_key")
            }
            child_story_keys.update({
                linked_issue.get("key")
                for linked_issue in (linked_by_cr.get(cr_key) or [])
                if linked_issue.get("key") and linked_issue.get("key") in direct_issue_by_key and _issue_type_slug(direct_issue_by_key.get(linked_issue.get("key"))) == "story"
            })

            for story_key in sorted(child_story_keys):
                story_issue = direct_issue_by_key.get(story_key)
                if not story_issue:
                    continue
                story_status_name = _issue_status_name(story_issue)
                _append_live_pair_row(
                    "cr_story",
                    cr_key=cr_key,
                    cr_status=cr_status_name,
                    story_key=story_key,
                    story_status=story_status_name,
                    verdict=compliance_rules.evaluate(cr_status_name, story_status_name),
                )

        for row in all_pair_rows:
            if row.get("cr_key"):
                pair_rows_by_key[row.get("cr_key")].append(row)
            if row.get("story_key"):
                pair_rows_by_key[row.get("story_key")].append(row)

        def _pair_detail_for_issue(issue_key, row):
            pair_type = row.get("pair_type")
            meta = pair_type_meta.get(pair_type, {"cr_label": "Issue", "story_label": "Issue", "rule_label": pair_type or "Compliance"})
            issue_is_cr_side = row.get("cr_key") == issue_key
            related_key = row.get("story_key") if issue_is_cr_side else row.get("cr_key")
            related_status = row.get("story_status") if issue_is_cr_side else row.get("cr_status")
            return {
                "pair_type": pair_type,
                "rule_label": meta.get("rule_label"),
                "issue_label": meta.get("cr_label") if issue_is_cr_side else meta.get("story_label"),
                "related_label": meta.get("story_label") if issue_is_cr_side else meta.get("cr_label"),
                "related_key": related_key,
                "related_status": related_status,
                "reason": row.get("reason"),
                "severity": row.get("severity"),
                "score": row.get("score"),
                "compliant": bool(row.get("compliant")),
            }

        def _decorate_issue(node):
            key = node.get("key")
            compliance_rules = [_pair_detail_for_issue(key, row) for row in pair_rows_by_key.get(key, [])]
            status_issues = [detail for detail in compliance_rules if not detail.get("compliant")]
            field_issues = list(field_findings_by_key.get(key, []))
            severity_candidates = [
                *(detail.get("severity") for detail in status_issues if detail.get("severity")),
                *(detail.get("severity") for detail in field_issues if detail.get("severity")),
            ]
            highest_severity = None
            if severity_candidates:
                highest_severity = max(severity_candidates, key=_issue_severity_rank)

            status_issue_count = len(status_issues)
            field_issue_count = len(field_issues)
            total_issue_count = status_issue_count + field_issue_count
            rule_coverage_count = len(compliance_rules)
            compliant_rule_count = sum(1 for detail in compliance_rules if detail.get("compliant"))

            if total_issue_count > 0:
                compliance_label = f"{highest_severity or 'Open'} · {total_issue_count} issue{'s' if total_issue_count != 1 else ''}"
            elif rule_coverage_count > 0:
                compliance_label = f"Compliant · {compliant_rule_count}/{rule_coverage_count} rules"
            else:
                compliance_label = "No rule coverage"

            node.update({
                "compliance_rules": compliance_rules,
                "status_issues": status_issues,
                "field_issues": field_issues,
                "status_issue_count": status_issue_count,
                "field_issue_count": field_issue_count,
                "total_issue_count": total_issue_count,
                "rule_coverage_count": rule_coverage_count,
                "compliant_rule_count": compliant_rule_count,
                "highest_severity": highest_severity,
                "compliance_label": compliance_label,
                "is_compliant": total_issue_count == 0,
            })
            return node

        epic_issue_nodes = [
            _decorate_issue(_shape_issue(direct_issue_by_key[key]))
            for key in sorted(direct_issue_by_key)
        ]
        epic_issue_nodes_by_key = {issue.get("key"): issue for issue in epic_issue_nodes if issue.get("key")}

        nested_issue_keys = set()
        issue_branches = []
        for cr_key in sorted(set(cr_issue_by_key) | epic_cr_keys):
            linked_nodes = []
            seen_for_cr = set()

            for row in cr_story_rows_by_cr.get(cr_key, []):
                linked_key = row.get("story_key")
                if not linked_key or linked_key == cr_key or linked_key in cr_issue_by_key or linked_key in seen_for_cr:
                    continue
                issue_node = epic_issue_nodes_by_key.get(linked_key)
                if not issue_node:
                    continue
                seen_for_cr.add(linked_key)
                nested_issue_keys.add(linked_key)
                linked_nodes.append({
                    **issue_node,
                    "relation": issue_node.get("relation") or "scan_related_to_cr",
                })

            for linked_issue in linked_by_cr.get(cr_key, []) or []:
                linked_key = linked_issue.get("key")
                if not linked_key or linked_key == cr_key or linked_key not in direct_issue_by_key or linked_key in cr_issue_by_key:
                    continue
                if linked_key in seen_for_cr:
                    continue
                seen_for_cr.add(linked_key)
                nested_issue_keys.add(linked_key)
                linked_nodes.append(_decorate_issue(_shape_issue(direct_issue_by_key[linked_key], relation="linked_to_cr")))

            branch_root = epic_issue_nodes_by_key.get(cr_key) or _decorate_issue(_shape_issue(cr_issue_by_key.get(cr_key) or direct_issue_by_key.get(cr_key) or _synthetic_issue(cr_key, "Change Request", supplemental_status_hints.get(cr_key)), relation="epic_child"))
            issue_branches.append({
                **branch_root,
                "linked_issues": sorted(linked_nodes, key=lambda item: item.get("key") or ""),
                "children": sorted(linked_nodes, key=lambda item: item.get("key") or ""),
                "linked_issue_count": len(linked_nodes),
            })

        remaining_issue_nodes = [
            issue
            for issue in epic_issue_nodes
            if issue.get("key") not in set(cr_issue_by_key) | epic_cr_keys and issue.get("key") not in nested_issue_keys
        ]

        issue_branches.extend({
            **issue,
            "linked_issues": [],
            "children": [],
            "linked_issue_count": 0,
        } for issue in sorted(remaining_issue_nodes, key=lambda item: item.get("key") or ""))

        issue_branches = sorted(issue_branches, key=lambda item: (str(item.get("issue_type") or ""), str(item.get("key") or "")))

        epic_node = _decorate_issue({
            "key": epic_key,
            "summary": epic_fields.get("summary") or epic_key,
            "status": _nested_name(epic_fields.get("status")),
            "issue_type": "Epic",
            "assignee": _nested_name(epic_fields.get("assignee")),
            "parent_key": outcome_key,
            "relation": "focus_epic",
            "children": issue_branches,
        })

        outcome_node = None
        if outcome_key:
            outcome_node = _decorate_issue({
                "key": outcome_key,
                "summary": outcome_fields.get("summary") or outcome_key,
                "status": _nested_name(outcome_fields.get("status")),
                "issue_type": "Outcome",
                "assignee": _nested_name(outcome_fields.get("assignee")),
                "parent_key": None,
                "relation": "parent_outcome",
                "children": [epic_node],
            })

        scope_keys = sorted({
            epic_key,
            *([outcome_key] if outcome_key else []),
            *direct_issue_by_key.keys(),
            *nested_issue_keys,
        })

        return {
            "epic_key": epic_key,
            "jql": {
                "exact": exact_jql,
                "cr_only": cr_only_jql,
                "linked_template": 'issue in linkedIssues("CR-KEY")',
            },
            "outcome": outcome_node,
            "epic": {**epic_node, "labels": epic_fields.get("labels") or []},
            "direct_issues": epic_issue_nodes,
            "crs": [branch for branch in issue_branches if branch.get("is_cr")],
            "issue_branches": issue_branches,
            "remaining_issues": sorted(remaining_issue_nodes, key=lambda item: item.get("key") or ""),
            "direct_issue_map": epic_issue_nodes_by_key,
            "scope_keys": scope_keys,
            "summary": {
                "direct_issue_count": len(epic_issue_nodes),
                "cr_count": len([branch for branch in issue_branches if branch.get("is_cr")]),
                "branch_count": len(issue_branches),
                "linked_issue_count": len(nested_issue_keys),
                "remaining_issue_count": len(remaining_issue_nodes),
                "direct_issue_open_count": len([issue for issue in epic_issue_nodes if issue.get("total_issue_count")]),
                "query_time_ms": int((time.perf_counter() - started_at) * 1000),
                "run_id": latest_run_id,
            },
        }

    result = run_async(run_focus())
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/hierarchy/trace", methods=["GET"])
def hierarchy_trace():
    """
    Full hierarchy traversal with compliance checking for a specific label.
    Query params:
      - label: Optional label to filter by (e.g., "AHRO", "ASRO")
      - quick: If true, skip field validation for speed
    Returns: Outcome -> Epic -> CR -> Story hierarchy with status/field issues.
    """
    if not _has_runtime_jira_credentials():
        return jsonify({"error": _runtime_jira_error_message()}), 401

    label = request.args.get("label")
    quick_mode = request.args.get("quick", "false").lower() == "true"

    async def run_trace():
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tracer = HierarchyTracer(
                    session, jira_rest, compliance_rules, field_rules, app.logger
                )
                hierarchy = await tracer.get_hierarchy_by_label(label)

                field_findings_by_key = defaultdict(list)
                for finding in compliance_db.get_latest_field_findings():
                    entity_key = finding.get("entity_key")
                    if not entity_key:
                        continue

                    field_findings_by_key[entity_key].append({
                        "field": finding.get("field"),
                        "severity": finding.get("severity"),
                        "message": finding.get("message"),
                        "status": finding.get("entity_status"),
                    })

                status_mismatch_count = 0
                field_gap_count = 0

                def annotate_node(node):
                    nonlocal status_mismatch_count, field_gap_count

                    if not isinstance(node, dict):
                        return

                    key = node.get("key")
                    node["fieldIssues"] = field_findings_by_key.get(key, [])
                    field_gap_count += len(node["fieldIssues"])
                    status_mismatch_count += len(node.get("statusIssues", []))

                    for child_key in ["outcomes", "epics", "crs", "stories"]:
                        for child in node.get(child_key, []) or []:
                            annotate_node(child)

                for outcome in hierarchy.get("outcomes", []):
                    annotate_node(outcome)

                hierarchy.setdefault("summary", {})["status_mismatches"] = status_mismatch_count
                hierarchy["summary"]["field_gaps"] = field_gap_count

                return hierarchy

    result = run_async(run_trace())
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/hierarchy/labels", methods=["GET"])
def hierarchy_labels():
    """List all available labels with outcome counts."""
    if not _has_runtime_jira_credentials():
        return jsonify({"error": _runtime_jira_error_message()}), 401

    async def run_trace():
        async with sse_client(MCP_SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tracer = HierarchyTracer(
                    session, jira_rest, compliance_rules, field_rules, app.logger
                )
                summary = await tracer.get_hierarchy_summary()

                # Extract unique labels from the summary
                labels_dict = summary.get("labels", {})
                labels_list = [
                    {
                        "name": label,
                        "outcome_count": data.get("outcomes", 0),
                        "epic_count": data.get("epics", 0),
                        "cr_count": data.get("crs", 0),
                    }
                    for label, data in sorted(labels_dict.items())
                    if label != "_all"
                ]

                return {
                    "labels": labels_list,
                    "total_labels": len(labels_list),
                    "query_time_ms": summary.get("query_time_ms", 0),
                }

    result = run_async(run_trace())
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/hierarchy/compliance-issues", methods=["GET"])
def hierarchy_compliance_issues():
    """
    Fast scan for compliance issues (status mismatches + field gaps).
    Uses existing scan data when available for speed.
    Query params:
      - level: "outcome", "epic", "cr", or "all" (default)
      - severity: "Critical", "High", "Medium", "Low", or "all"
    """
    if not _has_runtime_jira_credentials():
        return jsonify({"error": _runtime_jira_error_message()}), 401

    level = request.args.get("level", "all")
    severity_filter = request.args.get("severity", "all")

    # Use existing scan data from the database for speed
    run_id = compliance_db.latest_run_id()
    if not run_id:
        return jsonify({
            "status_mismatches": [],
            "field_gaps": [],
            "total_issues": 0,
            "query_time_ms": 0,
            "message": "No scan data available. Run a full scan first.",
        })

    start_time = __import__("time").time()

    # Get existing scan results
    all_pair_rows = compliance_db.get_pair_findings(run_id, "cr_story", include_compliant=False)
    all_pair_rows += compliance_db.get_pair_findings(run_id, "epic_cr", include_compliant=False)
    all_pair_rows += compliance_db.get_pair_findings(run_id, "story_outcome", include_compliant=False)
    all_pair_rows += compliance_db.get_pair_findings(run_id, "outcome_epic", include_compliant=False)

    all_field_findings = compliance_db.get_latest_field_findings()

    # Filter by level and severity
    status_mismatches = []
    for row in all_pair_rows:
        pair_type = row.get("pair_type")
        if level != "all":
            # Map pair_type to level
            type_to_level = {
                "cr_story": "cr",
                "epic_cr": "epic",
                "story_outcome": "outcome",
                "outcome_epic": "outcome",
            }
            if type_to_level.get(pair_type) != level:
                continue

        if severity_filter != "all" and row.get("severity") != severity_filter:
            continue

        status_mismatches.append({
            "key": row.get("cr_key"),
            "related_key": row.get("story_key"),
            "type": pair_type,
            "severity": row.get("severity"),
            "reason": row.get("reason"),
            "status": row.get("cr_status"),
            "related_status": row.get("story_status"),
        })

    field_gaps = []
    for finding in all_field_findings:
        entity_type = finding.get("entity_type")
        if level != "all" and entity_type != level:
            continue

        if severity_filter != "all" and finding.get("severity") != severity_filter:
            continue

        field_gaps.append({
            "key": finding.get("entity_key"),
            "type": entity_type,
            "severity": finding.get("severity"),
            "field": finding.get("field"),
            "message": finding.get("message"),
            "status": finding.get("entity_status"),
        })

    elapsed = (__import__("time").time() - start_time) * 1000

    return jsonify({
        "status_mismatches": status_mismatches,
        "field_gaps": field_gaps,
        "total_issues": len(status_mismatches) + len(field_gaps),
        "query_time_ms": int(elapsed),
        "run_id": run_id,
    })


@app.route("/api/hierarchy/drill-down", methods=["GET"])
def hierarchy_drill_down():
    """
    Drill down into a specific hierarchy level to see detailed compliance info.
    Query params:
      - key: The issue key (e.g., "OUTCOME-1", "EPIC-2", "CR-3")
      - include_children: If true, include all child hierarchy (default false)
    """
    if not _has_runtime_jira_credentials():
        return jsonify({"error": _runtime_jira_error_message()}), 401

    issue_key = request.args.get("key")
    include_children = request.args.get("include_children", "false").lower() == "true"

    if not issue_key:
        return jsonify({"error": "Provide ?key=ISSUE-KEY"}), 400

    run_id = compliance_db.latest_run_id()
    if not run_id:
        return jsonify({"error": "No scan data available"}), 404

    # Get issue description
    try:
        raw = jira_rest.get_issue_raw(issue_key)
    except Exception as e:
        return jsonify({"error": f"Could not fetch issue: {e}"}), 500

    fields = raw.get("fields", {})
    issue_type = fields.get("issuetype", {}).get("name", "Unknown")

    result = {
        "key": issue_key,
        "type": issue_type,
        "summary": fields.get("summary"),
        "description": field_rules._description_text(fields.get("description")),
        "status": (fields.get("status") or {}).get("name"),
        "labels": fields.get("labels", []),
        "linked_issues": [],
        "field_issues": [],
        "status_mismatches": [],
    }

    # Get field findings for this entity
    all_field_findings = compliance_db.get_latest_field_findings()
    for finding in all_field_findings:
        if finding.get("entity_key") == issue_key:
            result["field_issues"].append({
                "field": finding.get("field"),
                "severity": finding.get("severity"),
                "message": finding.get("message"),
            })

    # Get related issues (linked, parent, child)
    all_pairs = compliance_db.get_pair_findings(run_id, "cr_story", include_compliant=True)
    all_pairs += compliance_db.get_pair_findings(run_id, "epic_cr", include_compliant=True)
    all_pairs += compliance_db.get_pair_findings(run_id, "story_outcome", include_compliant=True)
    all_pairs += compliance_db.get_pair_findings(run_id, "outcome_epic", include_compliant=True)

    for pair in all_pairs:
        if pair.get("cr_key") == issue_key or pair.get("story_key") == issue_key:
            related = {
                "key": pair.get("story_key") if pair.get("cr_key") == issue_key else pair.get("cr_key"),
                "status": pair.get("story_status") if pair.get("cr_key") == issue_key else pair.get("cr_status"),
                "compliant": pair.get("compliant"),
                "reason": pair.get("reason"),
                "severity": pair.get("severity"),
            }
            if not pair.get("compliant"):
                result["status_mismatches"].append(related)
            result["linked_issues"].append(related)

    return jsonify(result)


@app.route("/")
def index():
    react_dist = os.path.join(app.static_folder, "react")
    react_index = os.path.join(react_dist, "index.html")
    legacy_index = os.path.join(app.static_folder, "index.html")

    if os.path.exists(react_index):
        return send_from_directory(react_dist, "index.html")

    if os.path.exists(legacy_index):
        return send_from_directory(app.static_folder, "index.html")

    return (
        "Frontend build not found. Build the React app first so / can serve "
        "static/react/index.html.",
        503,
    )


@app.route("/dashboard1")
def dashboard1():
    react_dist = os.path.join(app.static_folder, "react")
    react_index = os.path.join(react_dist, "index.html")

    if os.path.exists(react_index):
        return send_from_directory(react_dist, "index.html")

    return (
        "Frontend build not found. Build the React app first so /dashboard "
        "can serve static/react/index.html.",
        503,
    )


@app.route("/dashboard")
def dashboard():
    return send_from_directory(os.path.join(app.static_folder, "react"), "index.html")


@app.route("/landing")
def landing_page():
    return send_from_directory(os.path.join(app.static_folder, "react"), "index.html")


if __name__ == "__main__":
    #start_scheduler()

    # avoid starting the scheduler twice under Flask's debug reloader
    #if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and not app.debug:
        #start_scheduler()
    #elif os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        #start_scheduler()
    # threaded=True lets the server handle multiple requests at once —
    # without it, the dashboard's 6 parallel fetch() calls (loadAll())
    # get serialized one at a time server-side, adding their individual
    # latencies together instead of overlapping. This was the actual
    # cause of "scan finishes fine, but the display update lags."
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        threaded=True
    )