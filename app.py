import asyncio
import os
import threading

from flask import Flask, jsonify, request, send_from_directory
from mcp import ClientSession
from mcp.client.sse import sse_client

app = Flask(__name__, static_folder="static", static_url_path="")

# URL of the jira-mcp container's SSE endpoint (set MCP_SERVER_URL env var to override)
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/sse")

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
        print("=== run_async error ===")
        traceback.print_exc()
        real_errors = _flatten(e)
        return {"error": "; ".join(real_errors)}


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
# Single source of truth for both the top-level discovery JQL (below) and
# filtering linked Stories by their key's project prefix (see
# _in_allowed_projects) — a CR/Epic/Outcome in ACC/ACBD can link to a
# Story in a totally different project, which the discovery JQL alone
# can't filter (linkedIssues() has no project clause), so linked results
# need their own explicit filter too.
ALLOWED_PROJECTS = [p.strip().upper() for p in os.environ.get("ALLOWED_PROJECTS", "ACC,ACBD").split(",") if p.strip()]
_ALLOWED_PROJECTS_JQL = "project in (" + ", ".join(f'"{p}"' for p in ALLOWED_PROJECTS) + ")"

CR_DISCOVERY_JQL = os.environ.get("CR_DISCOVERY_JQL", f'{_ALLOWED_PROJECTS_JQL} AND issuetype = "Change Request"')

# Scrum Team is a filter dimension, not a mandatory field check, so it
# lives here rather than in field_rules.py's CR/EPIC/OUTCOME_FIELDS.
SCRUM_TEAM_FIELD_ID = os.environ.get("SCRUM_TEAM_FIELD_ID", "customfield_21304")
OUTCOME_DISCOVERY_JQL = os.environ.get("OUTCOME_DISCOVERY_JQL", f'{_ALLOWED_PROJECTS_JQL} AND issuetype = "Outcome"')
EPIC_DISCOVERY_JQL = os.environ.get("EPIC_DISCOVERY_JQL", f'{_ALLOWED_PROJECTS_JQL} AND issuetype = "Epic"')


def _in_allowed_projects(issue_key):
    """Jira issue keys are always PROJECTKEY-NUMBER — this checks that
    prefix against ALLOWED_PROJECTS. Used to filter linked Stories,
    since linkedIssues() search has no project clause of its own (a CR
    in ACC/ACBD can legitimately link to a Story in a different
    project, which needs to be excluded explicitly here)."""
    if not issue_key or "-" not in issue_key:
        return False
    return issue_key.split("-")[0].upper() in ALLOWED_PROJECTS

# How many direct-REST fetches (jira_rest.get_issue_raw) run concurrently.
# These are blocking HTTP calls, moved off the event loop via
# asyncio.to_thread and batched — at 10k+ records, doing these one at a
# time sequentially was the dominant cost in scan time. Bounded rather
# than unlimited so we don't hammer Jira / trip rate limits.
CONCURRENT_FETCH_LIMIT = int(os.environ.get("CONCURRENT_FETCH_LIMIT", 8))


async def _search_all_issues(session, jql, max_pages=50):
    """jira_search only returns one page by default, which silently
    truncates results on any query returning more than a page's worth —
    a real correctness bug at 10k+ record scale, not just a performance
    one. Loops using the tool's next_page_token until exhausted (or
    max_pages, as a safety cap against an infinite loop if something's
    wrong), returning every matching issue."""
    import json as _json
    all_issues = []
    page_token = None
    for _ in range(max_pages):
        args = {"jql": jql}
        if page_token:
            args["page_token"] = page_token
        result = await session.call_tool("jira_search", args)
        page_token = None
        for block in [b.model_dump() for b in result.content]:
            text = block.get("text")
            if not text:
                continue
            try:
                parsed = _json.loads(text)
                all_issues.extend(parsed.get("issues", []))
                page_token = parsed.get("next_page_token")
            except Exception:
                pass
        if not page_token:
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
    if board_id:
        return await asyncio.to_thread(jira_rest.search_board_issues, board_id, base_jql)
    if project_key:
        scoped_jql = f'project = "{project_key}" AND ({base_jql})'
        return await _search_all_issues(session, scoped_jql)
    return await _search_all_issues(session, base_jql)


async def _linked_issues_concurrent(session, keys, limit=None):
    """Finds linked issues for multiple entities (CRs or Outcomes)
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
                print(f"[scan] could not fetch linked issues for {key}: {e}")
                results[key] = []

    await asyncio.gather(*(_one(k) for k in keys))
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
                print(f"[jira_rest] could not fetch {key}: {e}")
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
        print(f"[ai_classify] could not classify {status_type} status {raw_status!r}: {e}")
        return
    compliance_rules.learn_alias(status_type, raw_status, result["target"])
    compliance_db.save_status_mapping(
        status_type, raw_status, result["target"], "ai", result.get("reasoning", ""), result.get("confidence", "")
    )
    print(f"[ai_classify] learned: {status_type} '{raw_status}' -> '{result['target']}' ({result.get('confidence')})")


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
    epic_results = []
    outcome_results = []
    outcome_epic_results = []
    issue_summaries = {}  # issue_key -> summary/title, collected from every discovery point below
    issue_scrum_teams = {}  # issue_key -> Scrum Team value, collected from CR/Epic/Outcome raw data

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            crs = await _discover_issues(session, CR_DISCOVERY_JQL, project_key, board_id)
            issue_summaries.update({c.get("key"): c.get("summary") for c in crs})
            print(f"[scan] discovered {len(crs)} CRs, fetching field data concurrently...")
            cr_raw_by_key = await _fetch_raw_concurrent([cr.get("key") for cr in crs])
            print(f"[scan] fetching linked stories for {len(crs)} CRs concurrently...")
            cr_linked_by_key = await _linked_issues_concurrent(session, [cr.get("key") for cr in crs])
            for _linked_list in cr_linked_by_key.values():
                issue_summaries.update({i.get("key"): i.get("summary") for i in _linked_list})
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
                        epic_key = raw_fields.get(cr_field_map["epic_link"])
                    if epic_key:
                        epic_to_cr_entries.setdefault(epic_key, []).append((cr_key, cr_status))

                linked = [i for i in cr_linked_by_key.get(cr_key, []) if _in_allowed_projects(i.get("key"))]

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
            issue_summaries.update({e.get("key"): e.get("summary") for e in epics})
            print(f"[scan] discovered {len(epics)} Epics, fetching field data concurrently...")
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

                    # An Epic's parent is an Outcome — same "parent" field
                    # convention already used for CR's parent Epic lookup.
                    outcome_key = None
                    parent = epic_fields.get("parent")
                    if parent and isinstance(parent, dict):
                        outcome_key = parent.get("key")
                    if outcome_key:
                        outcome_to_epic_entries.setdefault(outcome_key, []).append((epic_key, epic_status))

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

            # Outcome field-completeness + Story <- Outcome phase alignment
            outcomes = await _discover_issues(session, OUTCOME_DISCOVERY_JQL, project_key, board_id)
            issue_summaries.update({o.get("key"): o.get("summary") for o in outcomes})
            print(f"[scan] discovered {len(outcomes)} Outcomes, fetching field data concurrently...")
            outcome_raw_by_key = await _fetch_raw_concurrent([o.get("key") for o in outcomes])
            print(f"[scan] fetching linked stories for {len(outcomes)} Outcomes concurrently...")
            outcome_linked_by_key = await _linked_issues_concurrent(session, [o.get("key") for o in outcomes])
            for _linked_list in outcome_linked_by_key.values():
                issue_summaries.update({i.get("key"): i.get("summary") for i in _linked_list})
            outcome_field_map = _effective_field_map("outcome")

            for outcome in outcomes:
                outcome_key = outcome.get("key")
                outcome_status = (outcome.get("status") or {}).get("name")
                _ensure_classified("outcome", outcome_status)

                raw = outcome_raw_by_key.get(outcome_key)
                if raw is not None:
                    outcome_findings = field_rules.check_outcome_fields(raw.get("fields", {}), outcome_status, field_map=outcome_field_map)
                    field_findings_by_entity.append(("outcome", outcome_key, outcome_status, outcome_findings))
                    scrum_team = field_rules._value(raw.get("fields", {}), SCRUM_TEAM_FIELD_ID)
                    if scrum_team:
                        issue_scrum_teams[outcome_key] = scrum_team

                outcome_linked = [i for i in outcome_linked_by_key.get(outcome_key, []) if _in_allowed_projects(i.get("key"))]

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

                # Outcome <- Epic bottleneck check: an Epic's parent is
                # an Outcome (additive to the Story<->Outcome relationship
                # above — a Story linking to an Outcome and an Epic's
                # parent being that same Outcome are two separate real
                # relationships, not alternatives to each other).
                epic_entries = outcome_to_epic_entries.get(outcome_key, [])
                epic_verdict = compliance_rules.evaluate_outcome_epic(outcome_status, epic_entries)
                if epic_verdict["bottleneck_epic_key"] is not None:
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
    for entity_type, entity_key, entity_status, findings in field_findings_by_entity:
        compliance_db.save_field_findings(field_run_id, entity_type, entity_key, entity_status, findings)

    elapsed = _time.time() - scan_start
    print(f"[scan] completed in {elapsed:.1f}s — "
          f"{len(set(r['cr_key'] for r in results))} CRs, {len(results)} story checks, "
          f"{len(epic_results)} epic checks, {len(outcome_results)} outcome checks, "
          f"{len(field_findings_by_entity)} entities field-checked")

    return {
        "crs_scanned": len(set(r["cr_key"] for r in results)),
        "stories_scanned": len(results),
        "epics_scanned": len(epic_results),
        "outcomes_scanned": len(outcome_results),
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
    return jsonify(compliance_db.get_pair_findings(run_id, "outcome_epic"))


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
    def _background_rescan():
        try:
            run_async(_run_full_scan())
        except Exception as e:
            print(f"[transition] background rescan after {issue_key} failed: {e}")

    threading.Thread(target=_background_rescan, daemon=True).start()

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
    body = request.get_json(silent=True) or {}
    project_key = (body.get("project_key") or "").strip() or None
    board_id = body.get("board_id") or None
    result = run_async(_run_full_scan(project_key=project_key, board_id=board_id))
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/config", methods=["GET"])
def get_frontend_config():
    jira_url = os.environ.get("JIRA_URL", "").rstrip("/")
    return jsonify({
        "jira_base": f"{jira_url}/browse/" if jira_url else "",
        "default_project_key": os.environ.get("DEFAULT_PROJECT_KEY", ""),
    })


@app.route("/api/jira/projects", methods=["GET"])
def list_projects():
    try:
        projects = jira_rest.get_projects()
    except Exception as e:
        return jsonify({"error": f"Could not fetch projects: {e}"}), 500
    return jsonify({"items": projects, "default_project_key": os.environ.get("DEFAULT_PROJECT_KEY", "")})


@app.route("/api/jira/boards", methods=["GET"])
def list_boards():
    project_key = request.args.get("project_key") or None
    try:
        boards = jira_rest.get_boards(project_key=project_key)
    except Exception as e:
        return jsonify({"error": f"Could not fetch boards: {e}"}), 500
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
    from apscheduler.schedulers.background import BackgroundScheduler
    interval_minutes = int(os.environ.get("SCAN_INTERVAL_MINUTES", 15))
    retention_days = int(os.environ.get("RETENTION_DAYS", 7))

    def scan_and_prune():
        run_async(_run_full_scan())
        pruned = compliance_db.prune_old_runs(keep_days=retention_days)
        if pruned["pruned_runs"] > 0:
            print(f"[retention] pruned {pruned['pruned_runs']} runs older than {retention_days} days")

    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_and_prune, "interval", minutes=interval_minutes)
    scheduler.add_job(compliance_db.vacuum, "interval", hours=24)
    scheduler.start()
    # Run once immediately on startup so the dashboard has data right away
    scan_and_prune()
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


# ---------------------------------------------------------------------------
# Serve the simple test UI
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/dashboard")
def dashboard():
    return send_from_directory(os.path.join(app.static_folder, "react"), "index.html")


if __name__ == "__main__":
    # avoid starting the scheduler twice under Flask's debug reloader
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and not app.debug:
        start_scheduler()
    elif os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()
    # threaded=True lets the server handle multiple requests at once —
    # without it, the dashboard's 6 parallel fetch() calls (loadAll())
    # get serialized one at a time server-side, adding their individual
    # latencies together instead of overlapping. This was the actual
    # cause of "scan finishes fine, but the display update lags."
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
