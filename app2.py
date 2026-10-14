import asyncio
import datetime
import json
import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from mcp import ClientSession
from mcp.client.sse import sse_client

import compliance as compliance_rules
import ai_classify
import ai_draft
import field_rules
import jira_rest


app = Flask(__name__, static_folder="static", static_url_path="")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MCP_SERVER_URL = os.environ.get(
    "MCP_SERVER_URL",
    "http://jira-mcp:8000/sse",
)

ALLOWED_BOARD_NAMES = [
    b.strip()
    for b in os.environ.get("ALLOWED_BOARD_NAMES", "ACC,ACBD").split(",")
    if b.strip()
]

CR_DISCOVERY_JQL = 'filter = "Asylum_CRs"'
EPIC_DISCOVERY_JQL = 'filter = "Asylum_Epics"'
OUTCOME_DISCOVERY_JQL = 'filter = "Asylum_Outcomes"'

SCRUM_TEAM_FIELD_ID = os.environ.get(
    "SCRUM_TEAM_FIELD_ID",
    "customfield_21304",
)

CONCURRENT_FETCH_LIMIT = int(
    os.environ.get("CONCURRENT_FETCH_LIMIT", 8)
)

# No database is used anywhere in this application.
# All dashboard data is fetched/calculated from Jira live.


# ---------------------------------------------------------------------------
# In-memory AI status mappings only.
# They disappear when the application restarts.
# ---------------------------------------------------------------------------
_IN_MEMORY_STATUS_MAPPINGS = {}


def _mapping_key(status_type, raw_status):
    return (
        str(status_type).strip().lower(),
        str(raw_status).strip().upper(),
    )


def _learn_status(status_type, raw_status, target_status):
    if raw_status and target_status:
        _IN_MEMORY_STATUS_MAPPINGS[
            _mapping_key(status_type, raw_status)
        ] = target_status
        compliance_rules.learn_alias(
            status_type,
            raw_status,
            target_status,
        )


def _ensure_classified(status_type, raw_status):
    """
    Classify an unknown Jira status using the local LLM.

    IMPORTANT:
    No mapping is saved to SQLite or any other DB.
    It only remains in memory for this application process.
    """
    if not raw_status or compliance_rules.is_known(
        status_type,
        raw_status,
    ):
        return

    mapping = _IN_MEMORY_STATUS_MAPPINGS.get(
        _mapping_key(status_type, raw_status)
    )
    if mapping:
        compliance_rules.learn_alias(
            status_type,
            raw_status,
            mapping,
        )
        return

    known_fn = {
        "cr": compliance_rules.known_cr_statuses,
        "story": compliance_rules.known_story_statuses,
        "epic": compliance_rules.known_epic_statuses,
        "outcome": compliance_rules.known_outcome_statuses,
    }.get(
        status_type,
        compliance_rules.known_story_statuses,
    )

    try:
        result = ai_classify.classify_status(
            raw_status,
            status_type,
            known_fn(),
        )
        target = result.get("target")
        if target:
            _learn_status(status_type, raw_status, target)
            print(
                f"[ai_classify] learned in memory: "
                f"{status_type} '{raw_status}' -> '{target}'"
            )
    except Exception as e:
        print(
            f"[ai_classify] could not classify "
            f"{status_type} status {raw_status!r}: {e}"
        )


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------
async def _call_mcp_tool(tool_name, arguments):
    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                tool_name,
                arguments,
            )
            return [
                block.model_dump()
                for block in result.content
            ]


async def _list_mcp_tools():
    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [
                tool.model_dump()
                for tool in tools.tools
            ]


def run_async(coro):
    import traceback

    def _flatten(exc, depth=0):
        if hasattr(exc, "exceptions") and depth < 5:
            leaves = []
            for sub in exc.exceptions:
                leaves.extend(
                    _flatten(sub, depth + 1)
                )
            return leaves

        return [
            f"{type(exc).__name__}: {exc}"
        ]

    try:
        return asyncio.run(coro)
    except Exception as e:
        print("=== run_async error ===")
        traceback.print_exc()
        return {
            "error": "; ".join(_flatten(e))
        }


# ---------------------------------------------------------------------------
# JSON / Jira parsing helpers
# ---------------------------------------------------------------------------
def _json_blocks(blocks):
    """
    Convert MCP content blocks into JSON objects where possible.
    """
    output = []

    for block in blocks:
        text = block.get("text")
        if not text:
            continue

        try:
            output.append(json.loads(text))
        except Exception:
            continue

    return output


def _first_json(blocks):
    parsed = _json_blocks(blocks)
    return parsed[0] if parsed else None


def _extract_key(content_blocks):
    for block in content_blocks:
        text = block.get("text")
        if not text:
            continue

        try:
            parsed = json.loads(text)
        except Exception:
            continue

        if isinstance(parsed, dict):
            if parsed.get("key"):
                return parsed["key"]

            issue = parsed.get("issue")
            if isinstance(issue, dict) and issue.get("key"):
                return issue["key"]

    return None


def _issue_type(issue):
    return (
        issue.get("issue_type")
        or issue.get("issuetype")
        or {}
    ).get("name")


def _issue_status(issue):
    return (
        issue.get("status")
        or {}
    ).get("name")


def _issue_summary(issue):
    return issue.get("summary") or ""


def _extract_links(parsed):
    if not isinstance(parsed, dict):
        return []

    return (
        parsed.get("links")
        or parsed.get("issuelinks")
        or []
    )


def _linked_issue_from_link(link):
    return (
        link.get("issue")
        or link.get("outward_issue")
        or link.get("inward_issue")
    )


def _is_change_request(issue):
    if not isinstance(issue, dict):
        return False

    issue_type = _issue_type(issue)
    if issue_type == "Change Request":
        return True

    summary = str(
        issue.get("summary") or ""
    ).upper()

    return (
        "CR FOR " in summary
        or summary.startswith("CR ")
    )


# ---------------------------------------------------------------------------
# Live Jira discovery
# ---------------------------------------------------------------------------
def _get_allowed_issue_keys(board_id=None, project_key=None):
    """
    Get issue keys from Jira Agile board membership.

    This is only used to restrict the scan to allowed boards.
    Nothing is saved to a DB.
    """
    allowed_keys = set()

    if board_id:
        print(
            f"[live] fetching issues from selected board {board_id}"
        )
        issues = jira_rest.search_board_issues(board_id)
        allowed_keys.update(
            issue.get("key")
            for issue in issues
            if issue.get("key")
        )
        print(
            f"[live] found {len(allowed_keys)} "
            f"issues on board {board_id}"
        )
        return allowed_keys

    boards = jira_rest.get_boards(
        project_key=project_key
    )

    matching_boards = [
        board
        for board in boards
        if board.get("name") in ALLOWED_BOARD_NAMES
    ]

    if not matching_boards:
        print(
            "[live] WARNING: no allowed boards found. "
            f"Allowed names={ALLOWED_BOARD_NAMES}"
        )
        return allowed_keys

    for board in matching_boards:
        board_id_value = board.get("id")
        if not board_id_value:
            continue

        try:
            issues = jira_rest.search_board_issues(
                board_id_value
            )
            allowed_keys.update(
                issue.get("key")
                for issue in issues
                if issue.get("key")
            )
        except Exception as e:
            print(
                f"[live] board {board_id_value} lookup failed: {e}"
            )

    return allowed_keys


async def _search_all_issues(
    session,
    jql,
    max_pages=250,
):
    """
    Live Jira search with pagination.
    """
    all_issues = []
    page_token = None

    for _ in range(max_pages):
        args = {"jql": jql}

        if page_token:
            args["page_token"] = page_token

        result = await session.call_tool(
            "jira_search",
            args,
        )

        next_token = None

        for block in [
            b.model_dump()
            for b in result.content
        ]:
            text = block.get("text")
            if not text:
                continue

            try:
                parsed = json.loads(text)
            except Exception:
                continue

            if isinstance(parsed, dict):
                all_issues.extend(
                    parsed.get("issues", [])
                )
                next_token = parsed.get(
                    "next_page_token"
                )

        page_token = next_token

        if not page_token:
            break

    return all_issues


async def _discover_issues(
    session,
    base_jql,
    project_key=None,
    board_id=None,
):
    """
    Discover directly from Jira.

    The saved Jira filters are intentionally used as the source:
      filter = "Asylum_CRs"
      filter = "Asylum_Epics"
      filter = "Asylum_Outcomes"

    project_key is applied as an additional JQL restriction.
    """
    scoped_jql = base_jql

    if project_key:
        scoped_jql = (
            f'project = "{project_key}" '
            f"AND ({base_jql})"
        )

    issues = await _search_all_issues(
        session,
        scoped_jql,
    )

    if board_id:
        allowed = await asyncio.to_thread(
            _get_allowed_issue_keys,
            board_id,
            project_key,
        )
        issues = [
            issue
            for issue in issues
            if issue.get("key") in allowed
        ]
    elif ALLOWED_BOARD_NAMES:
        allowed = await asyncio.to_thread(
            _get_allowed_issue_keys,
            None,
            project_key,
        )
        if allowed:
            issues = [
                issue
                for issue in issues
                if issue.get("key") in allowed
            ]

    return issues


async def _linked_issues_concurrent(
    session,
    keys,
    limit=None,
):
    sem = asyncio.Semaphore(
        limit or CONCURRENT_FETCH_LIMIT
    )
    results = {}

    async def _one(key):
        async with sem:
            try:
                results[key] = await _search_all_issues(
                    session,
                    f'issue in linkedIssues("{key}")',
                )
            except Exception as e:
                print(
                    f"[live] linked issue lookup failed "
                    f"for {key}: {e}"
                )
                results[key] = []

    await asyncio.gather(
        *(_one(key) for key in keys)
    )

    return results


async def _fetch_raw_concurrent(keys):
    sem = asyncio.Semaphore(
        CONCURRENT_FETCH_LIMIT
    )
    results = {}

    async def _one(key):
        async with sem:
            try:
                results[key] = await asyncio.to_thread(
                    jira_rest.get_issue_raw,
                    key,
                )
            except Exception as e:
                print(
                    f"[live] raw Jira lookup failed "
                    f"for {key}: {e}"
                )
                results[key] = None

    await asyncio.gather(
        *(_one(key) for key in keys)
    )

    return results


# ---------------------------------------------------------------------------
# Field configuration
# ---------------------------------------------------------------------------
_FIELD_MAPS = {
    "cr": (
        field_rules.CR_FIELDS,
        field_rules.CR_FIELD_LABELS,
        field_rules.CR_FIELD_TYPES,
    ),
    "epic": (
        field_rules.EPIC_FIELDS,
        field_rules.EPIC_FIELD_LABELS,
        field_rules.EPIC_FIELD_TYPES,
    ),
    "outcome": (
        field_rules.OUTCOME_FIELDS,
        field_rules.OUTCOME_FIELD_LABELS,
        field_rules.OUTCOME_FIELD_TYPES,
    ),
}

_CHECK_FUNCS = {
    "cr": field_rules.check_cr_fields,
    "epic": field_rules.check_epic_fields,
    "outcome": field_rules.check_outcome_fields,
}


def _effective_field_map(entity_type):
    return _FIELD_MAPS[entity_type][0]


# ---------------------------------------------------------------------------
# Live full scan
#
# IMPORTANT:
# This function does NOT save anything.
# It returns the complete current state directly from Jira.
# ---------------------------------------------------------------------------
async def _run_live_scan(
    project_key=None,
    board_id=None,
):
    started = time.time()

    results = []
    epic_results = []
    outcome_results = []
    outcome_epic_results = []

    field_findings = []
    issue_summaries = {}
    issue_scrum_teams = {}

    epic_to_cr_entries = {}
    outcome_to_epic_entries = {}

    async with sse_client(
        MCP_SERVER_URL
    ) as (read, write):
        async with ClientSession(
            read,
            write,
        ) as session:
            await session.initialize()

            allowed_keys = await asyncio.to_thread(
                _get_allowed_issue_keys,
                board_id,
                project_key,
            )

            print(
                f"[live] allowed issue count="
                f"{len(allowed_keys)}"
            )

            # ---------------------------------------------------------------
            # CRs
            # ---------------------------------------------------------------
            crs = await _discover_issues(
                session,
                CR_DISCOVERY_JQL,
                project_key,
                board_id,
            )

            issue_summaries.update({
                cr.get("key"): _issue_summary(cr)
                for cr in crs
                if cr.get("key")
            })

            cr_raw = await _fetch_raw_concurrent([
                cr.get("key")
                for cr in crs
                if cr.get("key")
            ])

            cr_linked = await _linked_issues_concurrent(
                session,
                [
                    cr.get("key")
                    for cr in crs
                    if cr.get("key")
                ],
            )

            for linked_list in cr_linked.values():
                for issue in linked_list:
                    if issue.get("key"):
                        issue_summaries[
                            issue["key"]
                        ] = _issue_summary(issue)

            cr_field_map = _effective_field_map("cr")

            for cr in crs:
                cr_key = cr.get("key")
                if not cr_key:
                    continue

                cr_status = _issue_status(cr)

                _ensure_classified(
                    "cr",
                    cr_status,
                )

                raw = cr_raw.get(cr_key)

                if raw is not None:
                    fields = raw.get(
                        "fields",
                        {},
                    )

                    findings = (
                        field_rules.check_cr_fields(
                            fields,
                            cr_status,
                            field_map=cr_field_map,
                        )
                    )

                    field_findings.append({
                        "entity_type": "cr",
                        "entity_key": cr_key,
                        "entity_status": cr_status,
                        "findings": findings,
                    })

                    scrum_team = field_rules._value(
                        fields,
                        SCRUM_TEAM_FIELD_ID,
                    )

                    if scrum_team:
                        issue_scrum_teams[
                            cr_key
                        ] = scrum_team

                    parent = fields.get("parent")
                    epic_key = None

                    if isinstance(parent, dict):
                        epic_key = parent.get("key")

                    if not epic_key:
                        epic_field = cr_field_map.get(
                            "epic_link"
                        )
                        if epic_field:
                            epic_key = fields.get(
                                epic_field
                            )

                    if epic_key:
                        epic_to_cr_entries.setdefault(
                            epic_key,
                            [],
                        ).append(
                            (
                                cr_key,
                                cr_status,
                            )
                        )

                linked = cr_linked.get(
                    cr_key,
                    [],
                )

                if allowed_keys:
                    linked = [
                        issue
                        for issue in linked
                        if issue.get("key")
                        in allowed_keys
                    ]

                for story in linked:
                    if _issue_type(story) != "Story":
                        continue

                    story_key = story.get("key")
                    story_status = _issue_status(story)

                    if not story_key:
                        continue

                    _ensure_classified(
                        "story",
                        story_status,
                    )

                    age_days = 0
                    updated = story.get("updated")

                    if updated:
                        try:
                            updated_dt = datetime.datetime.strptime(
                                updated[:19],
                                "%Y-%m-%d %H:%M:%S",
                            )
                            age_days = max(
                                0,
                                (
                                    datetime.datetime.utcnow()
                                    - updated_dt
                                ).days,
                            )
                        except Exception:
                            pass

                    verdict = (
                        compliance_rules.evaluate(
                            cr_status,
                            story_status,
                            age_days,
                        )
                    )

                    results.append({
                        "pair_type": "cr_story",
                        "cr_key": cr_key,
                        "cr_status": cr_status,
                        "story_key": story_key,
                        "story_status": story_status,
                        "compliant": verdict["compliant"],
                        "reason": verdict["reason"],
                        "severity": verdict["severity"],
                        "score": verdict["score"],
                        "story_summary": _issue_summary(story),
                        "cr_summary": _issue_summary(cr),
                    })

            # ---------------------------------------------------------------
            # Epics
            # ---------------------------------------------------------------
            epics = await _discover_issues(
                session,
                EPIC_DISCOVERY_JQL,
                project_key,
                board_id,
            )

            issue_summaries.update({
                epic.get("key"): _issue_summary(epic)
                for epic in epics
                if epic.get("key")
            })

            epic_raw = await _fetch_raw_concurrent([
                epic.get("key")
                for epic in epics
                if epic.get("key")
            ])

            epic_field_map = _effective_field_map(
                "epic"
            )

            for epic in epics:
                epic_key = epic.get("key")
                if not epic_key:
                    continue

                epic_status = _issue_status(epic)

                _ensure_classified(
                    "epic",
                    epic_status,
                )

                raw = epic_raw.get(epic_key)

                if raw is not None:
                    fields = raw.get(
                        "fields",
                        {},
                    )

                    findings = (
                        field_rules.check_epic_fields(
                            fields,
                            epic_status,
                            field_map=epic_field_map,
                        )
                    )

                    field_findings.append({
                        "entity_type": "epic",
                        "entity_key": epic_key,
                        "entity_status": epic_status,
                        "findings": findings,
                    })

                    scrum_team = field_rules._value(
                        fields,
                        SCRUM_TEAM_FIELD_ID,
                    )

                    if scrum_team:
                        issue_scrum_teams[
                            epic_key
                        ] = scrum_team

                    parent = fields.get("parent")

                    if isinstance(parent, dict):
                        outcome_key = parent.get(
                            "key"
                        )
                        if outcome_key:
                            outcome_to_epic_entries.setdefault(
                                outcome_key,
                                [],
                            ).append(
                                (
                                    epic_key,
                                    epic_status,
                                )
                            )

                cr_entries = epic_to_cr_entries.get(
                    epic_key,
                    [],
                )

                verdict = (
                    compliance_rules.evaluate_epic(
                        epic_status,
                        cr_entries,
                    )
                )

                if (
                    verdict.get(
                        "bottleneck_cr_key"
                    )
                    is not None
                ):
                    epic_results.append({
                        "pair_type": "epic_cr",
                        "cr_key": verdict[
                            "bottleneck_cr_key"
                        ],
                        "cr_status": verdict[
                            "bottleneck_cr_status"
                        ],
                        "story_key": epic_key,
                        "story_status": epic_status,
                        "compliant": verdict[
                            "compliant"
                        ],
                        "reason": verdict["reason"],
                        "severity": verdict[
                            "severity"
                        ],
                        "score": verdict["score"],
                        "story_summary": _issue_summary(
                            epic
                        ),
                    })

            # ---------------------------------------------------------------
            # Outcomes
            # ---------------------------------------------------------------
            outcomes = await _discover_issues(
                session,
                OUTCOME_DISCOVERY_JQL,
                project_key,
                board_id,
            )

            issue_summaries.update({
                outcome.get("key"): _issue_summary(outcome)
                for outcome in outcomes
                if outcome.get("key")
            })

            outcome_raw = await _fetch_raw_concurrent([
                outcome.get("key")
                for outcome in outcomes
                if outcome.get("key")
            ])

            outcome_linked = await _linked_issues_concurrent(
                session,
                [
                    outcome.get("key")
                    for outcome in outcomes
                    if outcome.get("key")
                ],
            )

            for linked_list in outcome_linked.values():
                for issue in linked_list:
                    if issue.get("key"):
                        issue_summaries[
                            issue["key"]
                        ] = _issue_summary(issue)

            outcome_field_map = _effective_field_map(
                "outcome"
            )

            for outcome in outcomes:
                outcome_key = outcome.get("key")
                if not outcome_key:
                    continue

                outcome_status = _issue_status(
                    outcome
                )

                _ensure_classified(
                    "outcome",
                    outcome_status,
                )

                raw = outcome_raw.get(
                    outcome_key
                )

                if raw is not None:
                    fields = raw.get(
                        "fields",
                        {},
                    )

                    findings = (
                        field_rules.check_outcome_fields(
                            fields,
                            outcome_status,
                            field_map=outcome_field_map,
                        )
                    )

                    field_findings.append({
                        "entity_type": "outcome",
                        "entity_key": outcome_key,
                        "entity_status": outcome_status,
                        "findings": findings,
                    })

                    scrum_team = field_rules._value(
                        fields,
                        SCRUM_TEAM_FIELD_ID,
                    )

                    if scrum_team:
                        issue_scrum_teams[
                            outcome_key
                        ] = scrum_team

                linked = outcome_linked.get(
                    outcome_key,
                    [],
                )

                if allowed_keys:
                    linked = [
                        issue
                        for issue in linked
                        if issue.get("key")
                        in allowed_keys
                    ]

                for story in linked:
                    if _issue_type(story) != "Story":
                        continue

                    story_key = story.get("key")
                    story_status = _issue_status(
                        story
                    )

                    if not story_key:
                        continue

                    _ensure_classified(
                        "story",
                        story_status,
                    )

                    verdict = (
                        compliance_rules.evaluate_outcome(
                            story_status,
                            outcome_status,
                        )
                    )

                    outcome_results.append({
                        "pair_type": "story_outcome",
                        "cr_key": story_key,
                        "cr_status": story_status,
                        "story_key": outcome_key,
                        "story_status": outcome_status,
                        "compliant": verdict["compliant"],
                        "reason": verdict["reason"],
                        "severity": verdict["severity"],
                        "score": verdict["score"],
                        "story_summary": _issue_summary(
                            outcome
                        ),
                        "linked_story_summary": _issue_summary(
                            story
                        ),
                    })

                epic_entries = outcome_to_epic_entries.get(
                    outcome_key,
                    [],
                )

                epic_verdict = (
                    compliance_rules.evaluate_outcome_epic(
                        outcome_status,
                        epic_entries,
                    )
                )

                if (
                    epic_verdict.get(
                        "bottleneck_epic_key"
                    )
                    is not None
                ):
                    outcome_epic_results.append({
                        "pair_type": "outcome_epic",
                        "cr_key": epic_verdict[
                            "bottleneck_epic_key"
                        ],
                        "cr_status": epic_verdict[
                            "bottleneck_epic_status"
                        ],
                        "story_key": outcome_key,
                        "story_status": outcome_status,
                        "compliant": epic_verdict[
                            "compliant"
                        ],
                        "reason": epic_verdict[
                            "reason"
                        ],
                        "severity": epic_verdict[
                            "severity"
                        ],
                        "score": epic_verdict[
                            "score"
                        ],
                        "story_summary": _issue_summary(
                            outcome
                        ),
                    })

    elapsed = round(
        time.time() - started,
        1,
    )

    all_checks = (
        results
        + epic_results
        + outcome_results
        + outcome_epic_results
    )

    active = [
        row
        for row in all_checks
        if not row.get("compliant", False)
    ]

    return {
        "cr_story": results,
        "epic_cr": epic_results,
        "story_outcome": outcome_results,
        "outcome_epic": outcome_epic_results,
        "all": all_checks,
        "active": active,
        "field_findings": field_findings,
        "issue_summaries": issue_summaries,
        "issue_scrum_teams": issue_scrum_teams,
        "scan_seconds": elapsed,
        "counts": {
            "crs_scanned": len({
                r["cr_key"]
                for r in results
                if r.get("cr_key")
            }),
            "stories_scanned": len(results),
            "epics_scanned": len(epics),
            "outcomes_scanned": len(outcomes),
            "field_entities_scanned": len(
                field_findings
            ),
            "field_findings_total": sum(
                len(item["findings"])
                for item in field_findings
            ),
            "total_checks": len(all_checks),
            "non_compliant": len(active),
        },
    }


def _live_scan(project_key=None, board_id=None):
    return run_async(
        _run_live_scan(
            project_key=project_key,
            board_id=board_id,
        )
    )


# ---------------------------------------------------------------------------
# Discovery / generic MCP endpoints
# ---------------------------------------------------------------------------
@app.route("/api/tools", methods=["GET"])
def list_tools():
    result = run_async(_list_mcp_tools())

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify(result)


@app.route("/api/call/<tool_name>", methods=["POST"])
def call_tool_generic(tool_name):
    arguments = request.get_json(
        silent=True
    ) or {}

    result = run_async(
        _call_mcp_tool(
            tool_name,
            arguments,
        )
    )

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify(result)


# ---------------------------------------------------------------------------
# Basic Jira endpoints
# ---------------------------------------------------------------------------
@app.route("/api/issues/<issue_key>", methods=["GET"])
def get_issue(issue_key):
    result = run_async(
        _call_mcp_tool(
            "jira_get_issue",
            {"issue_key": issue_key},
        )
    )

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify(result)


@app.route("/api/search", methods=["GET"])
def search_issues():
    jql = request.args.get("jql", "")

    if not jql:
        return jsonify({
            "error": "Provide a jql query param"
        }), 400

    result = run_async(
        _call_mcp_tool(
            "jira_search",
            {"jql": jql},
        )
    )

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify(result)


@app.route("/api/issues", methods=["POST"])
def create_issue():
    body = request.get_json(
        silent=True
    ) or {}

    required = [
        "project_key",
        "summary",
        "issue_type",
    ]

    missing = [
        field
        for field in required
        if field not in body
    ]

    if missing:
        return jsonify({
            "error": f"Missing required fields: {missing}"
        }), 400

    result = run_async(
        _call_mcp_tool(
            "jira_create_issue",
            {
                "project_key": body["project_key"],
                "summary": body["summary"],
                "issue_type": body["issue_type"],
                "description": body.get(
                    "description",
                    "",
                ),
            },
        )
    )

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify(result)


@app.route(
    "/api/issues/<issue_key>/comment",
    methods=["POST"],
)
def add_comment(issue_key):
    body = request.get_json(
        silent=True
    ) or {}

    comment = body.get("comment")

    if not comment:
        return jsonify({
            "error": "Provide 'comment'"
        }), 400

    result = run_async(
        _call_mcp_tool(
            "jira_add_comment",
            {
                "issue_key": issue_key,
                "comment": comment,
            },
        )
    )

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify(result)


# ---------------------------------------------------------------------------
# Scenario creation
# ---------------------------------------------------------------------------
def _find_tool_name(all_tools, must_contain):
    for tool in all_tools:
        name = tool.get("name", "")
        if all(
            value in name
            for value in must_contain
        ):
            return name
    return None


@app.route(
    "/api/scenario/discover",
    methods=["GET"],
)
def discover_scenario_tools():
    tools = run_async(_list_mcp_tools())

    if isinstance(tools, dict) and "error" in tools:
        return jsonify(tools), 500

    keywords = [
        "create_issue",
        "link",
        "epic",
        "search",
        "get_issue",
    ]

    relevant = [
        tool
        for tool in tools
        if any(
            keyword in tool.get("name", "")
            for keyword in keywords
        )
    ]

    return jsonify(relevant)


@app.route(
    "/api/scenario/create",
    methods=["POST"],
)
def create_scenario():
    body = request.get_json(
        silent=True
    ) or {}

    project_key = body.get("project_key")
    epic_summary = body.get("epic_summary")
    stories = body.get("stories", [])
    cr_issue_type = body.get(
        "cr_issue_type",
        "Change Request",
    )
    link_type = body.get(
        "link_type",
        "relates to",
    )

    if not project_key or not epic_summary or not stories:
        return jsonify({
            "error": (
                "project_key, epic_summary, "
                "and at least one story are required"
            )
        }), 400

    all_tools = run_async(
        _list_mcp_tools()
    )

    if isinstance(all_tools, dict) and "error" in all_tools:
        return jsonify({
            "error": "Could not reach MCP server",
            "detail": all_tools,
        }), 500

    create_tool = (
        _find_tool_name(
            all_tools,
            ["create_issue"],
        )
        or "jira_create_issue"
    )

    if "link" in create_tool:
        create_tool = "jira_create_issue"

    link_tool = (
        _find_tool_name(
            all_tools,
            ["create", "link"],
        )
        or _find_tool_name(
            all_tools,
            ["link"],
        )
    )

    result = {
        "epic": None,
        "stories": [],
        "errors": [],
    }

    async def run_scenario():
        async with sse_client(
            MCP_SERVER_URL
        ) as (read, write):
            async with ClientSession(
                read,
                write,
            ) as session:
                await session.initialize()

                epic_res = await session.call_tool(
                    create_tool,
                    {
                        "project_key": project_key,
                        "summary": epic_summary,
                        "issue_type": "Epic",
                    },
                )

                epic_blocks = [
                    b.model_dump()
                    for b in epic_res.content
                ]

                result["epic"] = epic_blocks

                epic_key = _extract_key(
                    epic_blocks
                )

                if not epic_key:
                    result["errors"].append(
                        "Could not extract Epic key"
                    )
                    return

                for story in stories:
                    story_entry = {
                        "summary": story.get(
                            "summary"
                        ),
                        "story": None,
                        "cr": None,
                        "link": None,
                    }

                    story_res = await session.call_tool(
                        create_tool,
                        {
                            "project_key": project_key,
                            "summary": story.get(
                                "summary"
                            ),
                            "issue_type": "Story",
                            "description": story.get(
                                "description",
                                "",
                            ),
                            "additional_fields": {
                                "parent": {
                                    "key": epic_key
                                }
                            },
                        },
                    )

                    story_blocks = [
                        b.model_dump()
                        for b in story_res.content
                    ]

                    story_entry[
                        "story"
                    ] = story_blocks

                    story_key = _extract_key(
                        story_blocks
                    )

                    if story_key:
                        cr_res = await session.call_tool(
                            create_tool,
                            {
                                "project_key": project_key,
                                "summary": (
                                    f"CR for {story_key}: "
                                    f"{story.get('summary')}"
                                ),
                                "issue_type": cr_issue_type,
                                "description": (
                                    f"Change request associated "
                                    f"with {story_key}"
                                ),
                            },
                        )

                        cr_blocks = [
                            b.model_dump()
                            for b in cr_res.content
                        ]

                        story_entry[
                            "cr"
                        ] = cr_blocks

                        cr_key = _extract_key(
                            cr_blocks
                        )

                        if cr_key and link_tool:
                            try:
                                link_res = (
                                    await session.call_tool(
                                        link_tool,
                                        {
                                            "link_type": link_type,
                                            "inward_issue_key": story_key,
                                            "outward_issue_key": cr_key,
                                        },
                                    )
                                )

                                story_entry[
                                    "link"
                                ] = [
                                    b.model_dump()
                                    for b in link_res.content
                                ]

                            except Exception as e:
                                story_entry[
                                    "link"
                                ] = {
                                    "error": str(e)
                                }

                    result[
                        "stories"
                    ].append(story_entry)

    run_result = run_async(
        run_scenario()
    )

    if isinstance(run_result, dict) and "error" in run_result:
        return jsonify(run_result), 500

    return jsonify(result)


# ---------------------------------------------------------------------------
# Live mismatch endpoints
# ---------------------------------------------------------------------------
@app.route(
    "/api/scenario/mismatches",
    methods=["GET"],
)
def scenario_mismatches():
    epic_key = request.args.get(
        "epic_key"
    )

    if not epic_key:
        return jsonify({
            "error": "Provide ?epic_key=EPIC-1"
        }), 400

    async def run_check():
        async with sse_client(
            MCP_SERVER_URL
        ) as (read, write):
            async with ClientSession(
                read,
                write,
            ) as session:
                await session.initialize()

                stories = await _search_all_issues(
                    session,
                    (
                        f"parent = {epic_key} "
                        f"AND type = Story"
                    ),
                )

                checked = []
                mismatches = []

                for story in stories:
                    story_key = story.get("key")
                    story_status = _issue_status(
                        story
                    )

                    linked = await _search_all_issues(
                        session,
                        (
                            f'issue in linkedIssues('
                            f'"{story_key}")'
                        ),
                    )

                    crs = [
                        item
                        for item in linked
                        if _is_change_request(item)
                    ]

                    for cr in crs:
                        cr_key = cr.get("key")
                        cr_status = _issue_status(
                            cr
                        )

                        compliant, reason = (
                            compliance_rules.evaluate(
                                cr_status,
                                story_status,
                                0,
                            )["compliant"],
                            compliance_rules.evaluate(
                                cr_status,
                                story_status,
                                0,
                            )["reason"],
                        )

                        entry = {
                            "story_key": story_key,
                            "story_status": story_status,
                            "cr_key": cr_key,
                            "cr_status": cr_status,
                            "compliant": compliant,
                            "compliance_reason": reason,
                        }

                        checked.append(entry)

                        if not compliant:
                            mismatches.append(
                                entry
                            )

                return {
                    "checked": checked,
                    "mismatches": mismatches,
                }

    result = run_async(run_check())

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify(result)


@app.route(
    "/api/scenario/cr-mismatches",
    methods=["GET"],
)
def cr_mismatches():
    cr_key = request.args.get(
        "cr_key"
    )

    if not cr_key:
        return jsonify({
            "error": "Provide ?cr_key=CR-1"
        }), 400

    async def run_check():
        async with sse_client(
            MCP_SERVER_URL
        ) as (read, write):
            async with ClientSession(
                read,
                write,
            ) as session:
                await session.initialize()

                cr_blocks = await _call_mcp_tool(
                    "jira_get_issue",
                    {"issue_key": cr_key},
                )

                cr_parsed = _first_json(
                    cr_blocks
                )

                if not cr_parsed:
                    return {
                        "error": "Could not parse CR issue"
                    }

                cr_status = (
                    cr_parsed.get("status")
                    or {}
                ).get("name")

                linked_issues = (
                    await _search_all_issues(
                        session,
                        (
                            f'issue in linkedIssues('
                            f'"{cr_key}")'
                        ),
                    )
                )

                checked = []
                mismatches = []

                for issue in linked_issues:
                    if _issue_type(issue) != "Story":
                        continue

                    story_key = issue.get("key")
                    story_status = _issue_status(
                        issue
                    )

                    verdict = (
                        compliance_rules.evaluate(
                            cr_status,
                            story_status,
                            0,
                        )
                    )

                    entry = {
                        "story_key": story_key,
                        "story_status": story_status,
                        "cr_key": cr_key,
                        "cr_status": cr_status,
                        "compliance_reason": verdict[
                            "reason"
                        ],
                        "compliant": verdict[
                            "compliant"
                        ],
                    }

                    checked.append(entry)

                    if not verdict["compliant"]:
                        mismatches.append(entry)

                return {
                    "cr_key": cr_key,
                    "cr_status": cr_status,
                    "checked": checked,
                    "mismatches": mismatches,
                }

    result = run_async(run_check())

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify(result)


# ---------------------------------------------------------------------------
# Live dashboard endpoints
# ---------------------------------------------------------------------------
@app.route(
    "/api/dashboard/summary",
    methods=["GET"],
)
def dashboard_summary():
    project_key = request.args.get(
        "project_key"
    ) or None

    board_id = request.args.get(
        "board_id"
    ) or None

    scan = _live_scan(
        project_key,
        board_id,
    )

    if isinstance(scan, dict) and "error" in scan:
        return jsonify(scan), 500

    active = scan["active"]

    by_reason = {}
    by_severity = {}
    by_cr = {}

    for row in active:
        reason = row.get(
            "reason",
            "unknown",
        )
        severity = row.get(
            "severity",
            "unknown",
        )
        cr_key = row.get(
            "cr_key",
            "unknown",
        )

        by_reason[reason] = (
            by_reason.get(reason, 0) + 1
        )

        by_severity[severity] = (
            by_severity.get(severity, 0) + 1
        )

        by_cr[cr_key] = (
            by_cr.get(cr_key, 0) + 1
        )

    return jsonify({
        "active": active,
        "summary": {
            **scan["counts"],
            "by_reason": by_reason,
            "by_severity": by_severity,
            "by_cr": by_cr,
        },
        "scan_seconds": scan[
            "scan_seconds"
        ],
        "source": "live_jira",
    })


@app.route(
    "/api/dashboard/breakdown",
    methods=["GET"],
)
def dashboard_breakdown():
    project_key = request.args.get(
        "project_key"
    ) or None

    board_id = request.args.get(
        "board_id"
    ) or None

    scan = _live_scan(
        project_key,
        board_id,
    )

    if isinstance(scan, dict) and "error" in scan:
        return jsonify(scan), 500

    reason_counts = {}
    severity_counts = {}
    cr_counts = {}

    for row in scan["active"]:
        reason = row.get(
            "reason",
            "unknown",
        )
        severity = row.get(
            "severity",
            "unknown",
        )
        cr_key = row.get(
            "cr_key",
            "unknown",
        )

        reason_counts[reason] = (
            reason_counts.get(reason, 0) + 1
        )

        severity_counts[severity] = (
            severity_counts.get(severity, 0) + 1
        )

        cr_counts[cr_key] = (
            cr_counts.get(cr_key, 0) + 1
        )

    return jsonify({
        "by_reason": reason_counts,
        "by_severity": severity_counts,
        "by_cr": cr_counts,
        "source": "live_jira",
    })


@app.route(
    "/api/dashboard/trend",
    methods=["GET"],
)
def dashboard_trend():
    """
    No historical DB is maintained.

    Therefore this endpoint returns the current live snapshot instead
    of historical database data.
    """
    project_key = request.args.get(
        "project_key"
    ) or None

    board_id = request.args.get(
        "board_id"
    ) or None

    scan = _live_scan(
        project_key,
        board_id,
    )

    if isinstance(scan, dict) and "error" in scan:
        return jsonify(scan), 500

    now = datetime.datetime.utcnow().isoformat()

    return jsonify({
        "source": "live_jira",
        "historical_data": False,
        "trend": [{
            "date": now,
            "total": len(scan["all"]),
            "active": len(scan["active"]),
            "compliant": (
                len(scan["all"])
                - len(scan["active"])
            ),
        }],
    })


@app.route(
    "/api/dashboard/refresh",
    methods=["POST"],
)
def dashboard_refresh():
    body = request.get_json(
        silent=True
    ) or {}

    project_key = (
        body.get("project_key")
        or ""
    ).strip() or None

    board_id = (
        body.get("board_id")
        or None
    )

    scan = _live_scan(
        project_key,
        board_id,
    )

    if isinstance(scan, dict) and "error" in scan:
        return jsonify(scan), 500

    return jsonify({
        "refreshed": True,
        "source": "live_jira",
        "scan_seconds": scan[
            "scan_seconds"
        ],
        **scan["counts"],
    })


# ---------------------------------------------------------------------------
# Live issue data
# ---------------------------------------------------------------------------
@app.route(
    "/api/issues/summaries",
    methods=["GET"],
)
def get_issue_summaries():
    keys_param = request.args.get(
        "keys"
    )

    requested_keys = None

    if keys_param:
        requested_keys = {
            key.strip()
            for key in keys_param.split(",")
            if key.strip()
        }

    scan = _live_scan()

    if isinstance(scan, dict) and "error" in scan:
        return jsonify(scan), 500

    summaries = scan[
        "issue_summaries"
    ]

    if requested_keys is not None:
        summaries = {
            key: value
            for key, value in summaries.items()
            if key in requested_keys
        }

    return jsonify(summaries)


@app.route(
    "/api/issues/scrum-teams",
    methods=["GET"],
)
def get_issue_scrum_teams():
    keys_param = request.args.get(
        "keys"
    )

    requested_keys = None

    if keys_param:
        requested_keys = {
            key.strip()
            for key in keys_param.split(",")
            if key.strip()
        }

    scan = _live_scan()

    if isinstance(scan, dict) and "error" in scan:
        return jsonify(scan), 500

    teams = scan[
        "issue_scrum_teams"
    ]

    if requested_keys is not None:
        teams = {
            key: value
            for key, value in teams.items()
            if key in requested_keys
        }

    return jsonify(teams)


@app.route(
    "/api/issues/<issue_key>/description",
    methods=["GET"],
)
def get_issue_description(issue_key):
    try:
        raw = jira_rest.get_issue_raw(
            issue_key
        )
    except Exception as e:
        return jsonify({
            "error": f"Could not fetch issue: {e}"
        }), 500

    fields = raw.get(
        "fields",
        {},
    )

    return jsonify({
        "issue_key": issue_key,
        "summary": fields.get(
            "summary"
        ),
        "description": field_rules._description_text(
            fields.get("description")
        ),
        "source": "live_jira",
    })


# ---------------------------------------------------------------------------
# Live pair endpoints
# ---------------------------------------------------------------------------
def _get_live_pair_results(
    pair_type,
    project_key=None,
    board_id=None,
):
    scan = _live_scan(
        project_key,
        board_id,
    )

    if isinstance(scan, dict) and "error" in scan:
        return scan

    return scan[pair_type]


@app.route(
    "/api/pairs/epic-cr",
    methods=["GET"],
)
def get_epic_cr_findings():
    return jsonify(
        _get_live_pair_results(
            "epic_cr",
            request.args.get(
                "project_key"
            ) or None,
            request.args.get(
                "board_id"
            ) or None,
        )
    )


@app.route(
    "/api/pairs/outcome-epic",
    methods=["GET"],
)
def get_outcome_epic_findings():
    return jsonify(
        _get_live_pair_results(
            "outcome_epic",
            request.args.get(
                "project_key"
            ) or None,
            request.args.get(
                "board_id"
            ) or None,
        )
    )


@app.route(
    "/api/pairs/story-outcome",
    methods=["GET"],
)
def get_story_outcome_findings():
    return jsonify(
        _get_live_pair_results(
            "story_outcome",
            request.args.get(
                "project_key"
            ) or None,
            request.args.get(
                "board_id"
            ) or None,
        )
    )


# ---------------------------------------------------------------------------
# Live field findings
# ---------------------------------------------------------------------------
@app.route(
    "/api/fields/findings",
    methods=["GET"],
)
def get_field_findings():
    entity_type = request.args.get(
        "entity_type"
    )

    if entity_type and entity_type not in (
        "cr",
        "epic",
        "outcome",
    ):
        return jsonify({
            "error": (
                "entity_type must be "
                "cr, epic, or outcome"
            )
        }), 400

    scan = _live_scan()

    if isinstance(scan, dict) and "error" in scan:
        return jsonify(scan), 500

    findings = scan[
        "field_findings"
    ]

    if entity_type:
        findings = [
            item
            for item in findings
            if item["entity_type"]
            == entity_type
        ]

    return jsonify({
        "items": findings,
        "source": "live_jira",
    })


@app.route(
    "/api/entities/<entity_type>/<entity_key>/fields",
    methods=["GET"],
)
def get_entity_current_fields(
    entity_type,
    entity_key,
):
    if entity_type not in _FIELD_MAPS:
        return jsonify({
            "error": (
                "entity_type must be "
                "cr, epic, or outcome"
            )
        }), 400

    _, labels, types = _FIELD_MAPS[
        entity_type
    ]

    field_ids = _effective_field_map(
        entity_type
    )

    try:
        raw = jira_rest.get_issue_raw(
            entity_key
        )
    except Exception as e:
        return jsonify({
            "error": f"Could not fetch issue: {e}"
        }), 500

    fields = raw.get(
        "fields",
        {},
    )

    output = []

    for key, field_id in field_ids.items():
        value = field_rules._value(
            fields,
            field_id,
        )

        output.append({
            "key": key,
            "field_id": field_id,
            "label": labels.get(
                key,
                key,
            ),
            "type": types.get(
                key,
                "text",
            ),
            "value": value,
            "options": field_rules.SELECT_OPTIONS.get(
                key
            ),
        })

    return jsonify({
        "entity_key": entity_key,
        "fields": output,
        "source": "live_jira",
    })


@app.route(
    "/api/fields/check",
    methods=["POST"],
)
def check_single_issue_fields():
    body = request.get_json(
        silent=True
    ) or {}

    issue_key = body.get(
        "issue_key"
    )

    entity_type = body.get(
        "entity_type"
    )

    if not issue_key or entity_type not in (
        "cr",
        "epic",
        "outcome",
    ):
        return jsonify({
            "error": (
                "Provide issue_key and "
                "entity_type "
                "(cr, epic or outcome)"
            )
        }), 400

    try:
        raw = jira_rest.get_issue_raw(
            issue_key
        )
    except Exception as e:
        return jsonify({
            "error": f"Could not fetch issue: {e}"
        }), 500

    fields = raw.get(
        "fields",
        {},
    )

    status_name = (
        fields.get("status") or {}
    ).get("name")

    findings = _CHECK_FUNCS[
        entity_type
    ](
        fields,
        status_name,
        field_map=_effective_field_map(
            entity_type
        ),
    )

    return jsonify({
        "issue_key": issue_key,
        "status": status_name,
        "findings": findings,
        "source": "live_jira",
    })


# ---------------------------------------------------------------------------
# Jira transitions / updates
# ---------------------------------------------------------------------------
@app.route(
    "/api/jira/transitions/<issue_key>",
    methods=["GET"],
)
def get_transitions(issue_key):
    try:
        transitions = (
            jira_rest.get_available_transitions(
                issue_key
            )
        )
    except Exception as e:
        return jsonify({
            "error": f"Could not fetch transitions: {e}"
        }), 500

    return jsonify({
        "issue_key": issue_key,
        "transitions": transitions,
        "source": "live_jira",
    })


@app.route(
    "/api/jira/transition",
    methods=["POST"],
)
def apply_transition():
    body = request.get_json(
        silent=True
    ) or {}

    issue_key = body.get(
        "issue_key"
    )

    transition_id = body.get(
        "transition_id"
    )

    if not issue_key or not transition_id:
        return jsonify({
            "error": (
                "Provide issue_key and "
                "transition_id"
            )
        }), 400

    try:
        result = jira_rest.transition_issue(
            issue_key,
            transition_id,
        )
    except Exception as e:
        return jsonify({
            "error": f"Transition failed: {e}"
        }), 500

    # Nothing is patched in a DB because there is no DB.
    # The next dashboard request reads the new Jira state directly.
    return jsonify({
        "transitioned": True,
        "issue_key": issue_key,
        "source": "live_jira",
        **result,
    })


@app.route(
    "/api/jira/update-field",
    methods=["POST"],
)
def update_field():
    body = request.get_json(
        silent=True
    ) or {}

    issue_key = body.get(
        "issue_key"
    )
    entity_type = body.get(
        "entity_type"
    )
    field_key = body.get(
        "field_key"
    )
    value = body.get(
        "value"
    )

    if not all([
        issue_key,
        entity_type,
        field_key,
    ]):
        return jsonify({
            "error": (
                "Provide issue_key, "
                "entity_type, field_key"
            )
        }), 400

    if entity_type not in _FIELD_MAPS:
        return jsonify({
            "error": (
                "entity_type must be "
                "cr, epic, or outcome"
            )
        }), 400

    _, _, types = _FIELD_MAPS[
        entity_type
    ]

    field_ids = _effective_field_map(
        entity_type
    )

    if field_key not in field_ids:
        return jsonify({
            "error": (
                f"Unknown field_key "
                f"'{field_key}' for "
                f"{entity_type}"
            )
        }), 400

    field_id = field_ids[
        field_key
    ]

    field_type = types.get(
        field_key,
        "text",
    )

    shaped_value = (
        field_rules.format_field_value(
            field_key,
            field_type,
            value,
        )
    )

    try:
        result = (
            jira_rest.update_issue_fields(
                issue_key,
                {
                    field_id: shaped_value
                },
            )
        )
    except Exception as e:
        return jsonify({
            "error": (
                f"Update failed: {e}. "
                "If this is a select field, "
                "check Jira's real options."
            )
        }), 500

    return jsonify({
        "updated": True,
        "issue_key": issue_key,
        "field_key": field_key,
        "source": "live_jira",
        **result,
    })


# ---------------------------------------------------------------------------
# AI mappings - memory only, never DB
# ---------------------------------------------------------------------------
@app.route(
    "/api/mappings",
    methods=["GET"],
)
def get_mappings():
    return jsonify([
        {
            "status_type": key[0],
            "raw_status": key[1],
            "target_status": value,
            "source": "memory",
        }
        for key, value
        in _IN_MEMORY_STATUS_MAPPINGS.items()
    ])


@app.route(
    "/api/mappings/pending",
    methods=["GET"],
)
def get_pending_mappings():
    # There is no persistent DB, therefore there is no DB-backed
    # pending-mapping queue.
    return jsonify([])


@app.route(
    "/api/mappings/override",
    methods=["POST"],
)
def override_mapping():
    body = request.get_json(
        silent=True
    ) or {}

    status_type = body.get(
        "status_type"
    )
    raw_status = body.get(
        "raw_status"
    )
    target_status = body.get(
        "target_status"
    )

    if not all([
        status_type,
        raw_status,
        target_status,
    ]):
        return jsonify({
            "error": (
                "Provide status_type, "
                "raw_status, target_status"
            )
        }), 400

    _learn_status(
        status_type,
        raw_status,
        target_status,
    )

    return jsonify({
        "updated": True,
        "source": "memory",
    })


# ---------------------------------------------------------------------------
# AI comments
# ---------------------------------------------------------------------------
@app.route(
    "/api/ai/draft-comment",
    methods=["POST"],
)
def ai_draft_comment():
    body = request.get_json(
        silent=True
    ) or {}

    mode = body.get(
        "mode",
        "phase",
    )

    if mode == "fields":
        entity_key = body.get(
            "entity_key"
        )
        entity_status = body.get(
            "entity_status"
        )
        findings = body.get(
            "findings"
        ) or []

        if not entity_key or not findings:
            return jsonify({
                "error": (
                    "Provide entity_key "
                    "and findings"
                )
            }), 400

        try:
            draft = (
                ai_draft.draft_field_comment(
                    entity_key,
                    entity_status,
                    findings,
                )
            )
        except Exception as e:
            return jsonify({
                "error": (
                    f"Local LLM call failed: {e}. "
                    "Is Ollama running?"
                )
            }), 500

        return jsonify({
            "issue_key": entity_key,
            "draft": draft,
        })

    target_key = body.get(
        "target_key"
    )
    target_status = body.get(
        "target_status"
    )
    other_key = body.get(
        "other_key"
    )
    other_status = body.get(
        "other_status"
    )
    reason = body.get(
        "reason"
    )
    severity = body.get(
        "severity"
    )

    if not target_key or not other_key:
        return jsonify({
            "error": (
                "Provide target_key "
                "and other_key"
            )
        }), 400

    target_desc = ""

    try:
        raw = jira_rest.get_issue_raw(
            target_key
        )

        target_desc = (
            field_rules._description_text(
                raw.get(
                    "fields",
                    {},
                ).get(
                    "description"
                )
            )
        )
    except Exception:
        # Description enrichment is optional.
        pass

    try:
        draft = (
            ai_draft.draft_remediation_comment(
                target_key=target_key,
                target_status=target_status,
                target_desc=target_desc,
                other_key=other_key,
                other_status=other_status,
                reason=reason,
                severity=severity,
            )
        )
    except Exception as e:
        return jsonify({
            "error": (
                f"Local LLM call failed: {e}. "
                "Is Ollama running?"
            )
        }), 500

    return jsonify({
        "issue_key": target_key,
        "draft": draft,
    })


@app.route(
    "/api/ai/post-comment",
    methods=["POST"],
)
def ai_post_comment():
    body = request.get_json(
        silent=True
    ) or {}

    story_key = body.get(
        "story_key"
    )
    comment = body.get(
        "comment"
    )

    if not story_key or not comment:
        return jsonify({
            "error": (
                "Provide story_key "
                "and comment"
            )
        }), 400

    result = run_async(
        _call_mcp_tool(
            "jira_add_comment",
            {
                "issue_key": story_key,
                "comment": comment,
            },
        )
    )

    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 500

    return jsonify({
        "posted": True,
        "story_key": story_key,
        "result": result,
    })


# ---------------------------------------------------------------------------
# Jira projects / boards / config
# ---------------------------------------------------------------------------
@app.route(
    "/api/jira/projects",
    methods=["GET"],
)
def list_projects():
    try:
        projects = jira_rest.get_projects()
    except Exception as e:
        return jsonify({
            "error": f"Could not fetch projects: {e}"
        }), 500

    return jsonify({
        "items": projects,
        "default_project_key": os.environ.get(
            "DEFAULT_PROJECT_KEY",
            "ACC",
        ),
        "source": "live_jira",
    })


@app.route(
    "/api/jira/boards",
    methods=["GET"],
)
def list_boards():
    project_key = (
        request.args.get(
            "project_key"
        )
        or None
    )

    try:
        boards = jira_rest.get_boards(
            project_key=project_key
        )
    except Exception as e:
        return jsonify({
            "error": f"Could not fetch boards: {e}"
        }), 500

    return jsonify({
        "items": boards,
        "source": "live_jira",
    })


@app.route(
    "/api/config",
    methods=["GET"],
)
def get_frontend_config():
    jira_url = os.environ.get(
        "JIRA_URL",
        "",
    ).rstrip("/")

    default_project = os.environ.get(
        "DEFAULT_PROJECT_KEY",
        "ACC",
    )

    return jsonify({
        "jira_base": (
            f"{jira_url}/browse/"
            if jira_url
            else ""
        ),
        "default_project_key": default_project,
        "source": "live_jira",
    })


# ---------------------------------------------------------------------------
# Legacy resolve/unresolve endpoints
#
# These cannot persist state because the application intentionally has
# no database. Jira remains the source of truth.
# ---------------------------------------------------------------------------
@app.route(
    "/api/dashboard/resolve",
    methods=["POST"],
)
def dashboard_resolve():
    return jsonify({
        "resolved": False,
        "message": (
            "Resolve state is not persisted. "
            "Update the Jira issue itself; "
            "dashboard reads Jira live."
        ),
        "source": "live_jira",
    }), 409


@app.route(
    "/api/dashboard/unresolve",
    methods=["POST"],
)
def dashboard_unresolve():
    return jsonify({
        "resolved": False,
        "message": (
            "Resolve state is not persisted. "
            "Dashboard reads Jira live."
        ),
        "source": "live_jira",
    }), 409


# ---------------------------------------------------------------------------
# DB endpoints retained only for frontend compatibility.
# They explicitly report that DB storage is disabled.
# ---------------------------------------------------------------------------
@app.route(
    "/api/dashboard/db-health",
    methods=["GET"],
)
def db_health():
    return jsonify({
        "enabled": False,
        "size_bytes": 0,
        "size_mb": 0,
        "retention_days": 0,
        "source": "live_jira",
    })


@app.route(
    "/api/dashboard/prune",
    methods=["POST"],
)
def manual_prune():
    return jsonify({
        "enabled": False,
        "message": (
            "Database storage and retention "
            "are disabled."
        ),
        "source": "live_jira",
    })


# ---------------------------------------------------------------------------
# Serve UI
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    react_dist = os.path.join(app.static_folder, "react")
    react_index = os.path.join(react_dist, "index.html")
    legacy_index = os.path.join(app.static_folder, "index.html")

    if os.path.exists(react_index):
        return send_from_directory(
            react_dist,
            "index.html",
        )

    if os.path.exists(legacy_index):
        return send_from_directory(
            app.static_folder,
            "index.html",
        )

    return (
        "Frontend build not found. Build the React app first so / can serve "
        "static/react/index.html.",
        503,
    )


@app.route("/dashboard")
def dashboard():
    react_dist = os.path.join(app.static_folder, "react")
    react_index = os.path.join(react_dist, "index.html")

    if os.path.exists(react_index):
        return send_from_directory(
            react_dist,
            "index.html",
        )

    return (
        "Frontend build not found. Build the React app first so /dashboard "
        "can serve static/react/index.html.",
        503,
    )


# ---------------------------------------------------------------------------
# Main
#
# No scheduler is required because dashboard APIs query Jira live.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(
        "=================================================="
    )
    print(
        "Compliance dashboard starting"
    )
    print(
        "Data source: LIVE JIRA"
    )
    print(
        "Database: DISABLED"
    )
    print(
        f"MCP_SERVER_URL={MCP_SERVER_URL}"
    )
    print(
        f"ALLOWED_BOARD_NAMES={ALLOWED_BOARD_NAMES}"
    )
    print(
        f"CR filter={CR_DISCOVERY_JQL}"
    )
    print(
        f"EPIC filter={EPIC_DISCOVERY_JQL}"
    )
    print(
        f"OUTCOME filter={OUTCOME_DISCOVERY_JQL}"
    )
    print(
        "=================================================="
    )

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True,
    )
