"""SQLite persistence helpers for compliance scan and landing data."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path


DB_PATH = Path(os.environ.get("COMPLIANCE_DB_PATH", str(Path(__file__).with_name("compliance.db"))))


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_checks_pair_type(conn):
    columns = [row[1] for row in conn.execute("PRAGMA table_info(checks)").fetchall()]
    if "pair_type" not in columns:
        conn.execute("ALTER TABLE checks ADD COLUMN pair_type TEXT DEFAULT 'cr_story'")
        conn.execute("UPDATE checks SET pair_type = 'cr_story' WHERE pair_type IS NULL OR pair_type = ''")


def create_db():
    conn = get_conn()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            crs_checked INTEGER NOT NULL DEFAULT 0,
            stories_checked INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            pair_type TEXT NOT NULL DEFAULT 'cr_story',
            cr_key TEXT,
            cr_status TEXT,
            story_key TEXT,
            story_status TEXT,
            compliant INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            severity TEXT,
            score REAL,
            created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS field_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            entity_status TEXT,
            check_name TEXT NOT NULL,
            field TEXT,
            severity TEXT,
            message TEXT,
            checked_at REAL NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS resolutions (
            story_key TEXT NOT NULL,
            cr_key TEXT NOT NULL,
            resolved_at REAL NOT NULL,
            PRIMARY KEY (story_key, cr_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS status_mappings (
            status_type TEXT NOT NULL,
            raw_status TEXT NOT NULL,
            target_status TEXT NOT NULL,
            source TEXT,
            reasoning TEXT,
            confidence TEXT,
            created_at REAL NOT NULL,
            reviewed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (status_type, raw_status)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_summaries (
            issue_key TEXT PRIMARY KEY,
            summary TEXT,
            updated_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_scrum_teams (
            issue_key TEXT PRIMARY KEY,
            scrum_team TEXT,
            updated_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_labels (
            issue_key TEXT PRIMARY KEY,
            labels_json TEXT,
            updated_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_relationships (
            child_key TEXT NOT NULL,
            parent_key TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            updated_at REAL,
            PRIMARY KEY (child_key, parent_key, relationship_type)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discovered_field_mappings (
            entity_type TEXT NOT NULL,
            field_key TEXT NOT NULL,
            jira_field_id TEXT,
            jira_label TEXT,
            confidence TEXT,
            reasoning TEXT,
            created_at REAL NOT NULL,
            reviewed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (entity_type, field_key)
        )
        """
    )

    _ensure_checks_pair_type(conn)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_run ON checks(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_run_pair ON checks(run_id, pair_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_story_cr ON checks(story_key, cr_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_field_findings_run ON field_findings(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_field_findings_entity ON field_findings(entity_type, entity_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_issue_relationships_type_parent ON issue_relationships(relationship_type, parent_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_issue_relationships_type_child ON issue_relationships(relationship_type, child_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_mappings_reviewed ON status_mappings(reviewed)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_field_mappings_reviewed ON discovered_field_mappings(reviewed)")

    conn.commit()
    conn.close()


def init_db():
    create_db()


def prune_old_runs(keep_days=7):
    """Delete runs and run-scoped rows older than keep_days."""
    conn = get_conn()
    cutoff = time.time() - (keep_days * 86400)
    old_run_ids = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE started_at < ?", (cutoff,)).fetchall()]
    if not old_run_ids:
        conn.close()
        return {"pruned_runs": 0}

    placeholders = ",".join("?" * len(old_run_ids))
    conn.execute(f"DELETE FROM checks WHERE run_id IN ({placeholders})", old_run_ids)
    conn.execute(f"DELETE FROM field_findings WHERE run_id IN ({placeholders})", old_run_ids)
    conn.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", old_run_ids)
    conn.commit()
    conn.close()
    return {"pruned_runs": len(old_run_ids)}


def vacuum():
    conn = get_conn()
    conn.execute("VACUUM")
    conn.close()


def get_db_size_bytes():
    return os.path.getsize(DB_PATH) if DB_PATH.exists() else 0


def save_run(results):
    """Persist the main CR/story scan results and return the run id."""
    results = results or []
    run_id = str(time.time())
    now = time.time()
    crs_checked = len({r.get("cr_key") for r in results if r.get("cr_key")})
    stories_checked = len(results)

    conn = get_conn()
    conn.execute(
        "INSERT INTO runs (run_id, started_at, crs_checked, stories_checked) VALUES (?, ?, ?, ?)",
        (run_id, now, crs_checked, stories_checked),
    )
    for result in results:
        conn.execute(
            """
            INSERT INTO checks
                (run_id, pair_type, cr_key, cr_status, story_key, story_status, compliant, reason, severity, score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                result.get("pair_type", "cr_story"),
                result.get("cr_key"),
                result.get("cr_status"),
                result.get("story_key"),
                result.get("story_status"),
                int(bool(result.get("compliant"))),
                result.get("reason"),
                result.get("severity"),
                result.get("score", 0),
            ),
        )
    conn.commit()
    conn.close()
    return run_id


def save_additional_checks(run_id, results, pair_type):
    """Persist extra hierarchy checks for the same run without touching runs."""
    if not results:
        return
    now = time.time()
    conn = get_conn()
    for result in results:
        conn.execute(
            """
            INSERT INTO checks
                (run_id, pair_type, cr_key, cr_status, story_key, story_status, compliant, reason, severity, score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                pair_type,
                result.get("cr_key"),
                result.get("cr_status"),
                result.get("story_key"),
                result.get("story_status"),
                int(bool(result.get("compliant"))),
                result.get("reason"),
                result.get("severity"),
                result.get("score", 0),
            ),
        )
    conn.commit()
    conn.close()


def get_pair_findings(run_id, pair_type, include_compliant=False):
    conn = get_conn()
    if include_compliant:
        rows = conn.execute(
            "SELECT * FROM checks WHERE run_id = ? AND pair_type = ?",
            (run_id, pair_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM checks WHERE run_id = ? AND pair_type = ? AND compliant = 0",
            (run_id, pair_type),
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def latest_run_id():
    conn = get_conn()
    row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    conn.close()
    return row["run_id"] if row else None


def latest_field_run_id():
    """Return the most recent field_findings run id."""
    conn = get_conn()
    row = conn.execute("SELECT run_id FROM field_findings ORDER BY checked_at DESC LIMIT 1").fetchone()
    conn.close()
    return row["run_id"] if row else None


def get_resolved_keys():
    conn = get_conn()
    rows = conn.execute("SELECT story_key, cr_key FROM resolutions").fetchall()
    conn.close()
    return {(row["story_key"], row["cr_key"]) for row in rows}


def resolve(story_key, cr_key):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO resolutions (story_key, cr_key, resolved_at) VALUES (?, ?, ?)",
        (story_key, cr_key, time.time()),
    )
    conn.commit()
    conn.close()


def unresolve(story_key, cr_key):
    conn = get_conn()
    conn.execute("DELETE FROM resolutions WHERE story_key = ? AND cr_key = ?", (story_key, cr_key))
    conn.commit()
    conn.close()


def save_status_mapping(status_type, raw_status, target_status, source, reasoning, confidence):
    conn = get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO status_mappings
            (status_type, raw_status, target_status, source, reasoning, confidence, created_at, reviewed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            status_type,
            (raw_status or "").upper(),
            (target_status or "").upper(),
            source,
            reasoning,
            confidence,
            time.time(),
            1 if source == "human" else 0,
        ),
    )
    conn.commit()
    conn.close()


def save_field_findings(run_id, entity_type, entity_key, entity_status, findings):
    """Persist field-level compliance findings for a single entity."""
    if not findings:
        return
    now = time.time()
    conn = get_conn()
    for finding in findings:
        conn.execute(
            """
            INSERT INTO field_findings
                (run_id, entity_type, entity_key, entity_status, check_name, field, severity, message, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                entity_type,
                entity_key,
                entity_status,
                finding["check"],
                finding.get("field"),
                finding["severity"],
                finding["message"],
                now,
            ),
        )
    conn.commit()
    conn.close()


def replace_field_findings_for_entity(run_id, entity_type, entity_key, entity_status, findings):
    """Replace all stored field findings for one entity within a run."""
    now = time.time()
    conn = get_conn()
    conn.execute(
        "DELETE FROM field_findings WHERE run_id = ? AND entity_type = ? AND entity_key = ?",
        (run_id, entity_type, entity_key),
    )
    for finding in findings or []:
        conn.execute(
            """
            INSERT INTO field_findings
                (run_id, entity_type, entity_key, entity_status, check_name, field, severity, message, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                entity_type,
                entity_key,
                entity_status,
                finding["check"],
                finding.get("field"),
                finding["severity"],
                finding["message"],
                now,
            ),
        )
    conn.commit()
    conn.close()


def get_latest_field_findings(entity_type=None):
    conn = get_conn()
    row = conn.execute("SELECT run_id FROM field_findings ORDER BY checked_at DESC LIMIT 1").fetchone()
    if not row:
        conn.close()
        return []

    run_id = row["run_id"]
    if entity_type:
        rows = conn.execute(
            "SELECT * FROM field_findings WHERE run_id = ? AND entity_type = ?",
            (run_id, entity_type),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM field_findings WHERE run_id = ?", (run_id,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_all_status_mappings():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM status_mappings").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_unreviewed_status_mappings():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM status_mappings WHERE reviewed = 0").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_status_mapping_reviewed(status_type, raw_status):
    conn = get_conn()
    conn.execute(
        "UPDATE status_mappings SET reviewed = 1 WHERE status_type = ? AND raw_status = ?",
        (status_type, (raw_status or "").upper()),
    )
    conn.commit()
    conn.close()


def save_discovered_field_mapping(entity_type, field_key, jira_field_id, jira_label, confidence, reasoning):
    """Store an AI-suggested field mapping as unreviewed."""
    conn = get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO discovered_field_mappings
            (entity_type, field_key, jira_field_id, jira_label, confidence, reasoning, created_at, reviewed)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (entity_type, field_key, jira_field_id, jira_label, confidence, reasoning, time.time()),
    )
    conn.commit()
    conn.close()


def get_discovered_field_mappings(entity_type=None):
    conn = get_conn()
    if entity_type:
        rows = conn.execute(
            "SELECT * FROM discovered_field_mappings WHERE entity_type = ?",
            (entity_type,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM discovered_field_mappings").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_confirmed_field_mappings(entity_type=None):
    conn = get_conn()
    if entity_type:
        rows = conn.execute(
            "SELECT * FROM discovered_field_mappings WHERE entity_type = ? AND reviewed = 1",
            (entity_type,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM discovered_field_mappings WHERE reviewed = 1").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_field_mapping_reviewed(entity_type, field_key, approved_jira_field_id=None):
    conn = get_conn()
    if approved_jira_field_id:
        conn.execute(
            "UPDATE discovered_field_mappings SET jira_field_id = ?, reviewed = 1 WHERE entity_type = ? AND field_key = ?",
            (approved_jira_field_id, entity_type, field_key),
        )
    else:
        conn.execute(
            "UPDATE discovered_field_mappings SET reviewed = 1 WHERE entity_type = ? AND field_key = ?",
            (entity_type, field_key),
        )
    conn.commit()
    conn.close()


def delete_discovered_field_mapping(entity_type, field_key):
    conn = get_conn()
    conn.execute("DELETE FROM discovered_field_mappings WHERE entity_type = ? AND field_key = ?", (entity_type, field_key))
    conn.commit()
    conn.close()


def get_latest_summary():
    run_id = latest_run_id()
    if not run_id:
        return {
            "run_id": None,
            "crs_checked": 0,
            "stories_checked": 0,
            "non_compliant": 0,
            "compliance_rate": None,
            "active": [],
        }

    conn = get_conn()
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    checks = conn.execute(
        "SELECT * FROM checks WHERE run_id = ? AND pair_type = 'cr_story'",
        (run_id,),
    ).fetchall()
    conn.close()

    resolved = get_resolved_keys()
    active_non_compliant = [
        dict(check)
        for check in checks
        if not check["compliant"] and (check["story_key"], check["cr_key"]) not in resolved
    ]

    stories_checked = run["stories_checked"] or 1
    non_compliant_count = len(active_non_compliant)
    compliance_rate = round(((stories_checked - non_compliant_count) / stories_checked) * 100)

    return {
        "run_id": run_id,
        "checked_at": run["started_at"],
        "crs_checked": run["crs_checked"],
        "stories_checked": run["stories_checked"],
        "non_compliant": non_compliant_count,
        "compliance_rate": compliance_rate,
        "active": active_non_compliant,
    }


def get_trend(days=14):
    """Return the most recent `days` runs as a compliance trend."""
    conn = get_conn()
    runs = conn.execute(
        "SELECT run_id, started_at, stories_checked FROM runs ORDER BY started_at DESC LIMIT ?",
        (days,),
    ).fetchall()
    conn.close()

    resolved = get_resolved_keys()
    trend = []
    for run in reversed(runs):
        conn = get_conn()
        checks = conn.execute(
            "SELECT story_key, cr_key, compliant FROM checks WHERE run_id = ? AND pair_type = 'cr_story'",
            (run["run_id"],),
        ).fetchall()
        conn.close()
        non_compliant = sum(
            1 for check in checks if not check["compliant"] and (check["story_key"], check["cr_key"]) not in resolved
        )
        total = run["stories_checked"] or 1
        rate = round(((total - non_compliant) / total) * 100)
        trend.append({"run_id": run["run_id"], "checked_at": run["started_at"], "compliance_rate": rate})
    return trend


def save_issue_summaries(summaries):
    if not summaries:
        return
    now = time.time()
    conn = get_conn()
    for key, summary in summaries.items():
        if not key:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO issue_summaries (issue_key, summary, updated_at) VALUES (?, ?, ?)",
            (key, summary or "", now),
        )
    conn.commit()
    conn.close()


def get_issue_summaries(keys=None):
    conn = get_conn()
    if keys is not None:
        keys = list(keys)
        if not keys:
            conn.close()
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            f"SELECT issue_key, summary FROM issue_summaries WHERE issue_key IN ({placeholders})",
            keys,
        ).fetchall()
    else:
        rows = conn.execute("SELECT issue_key, summary FROM issue_summaries").fetchall()
    conn.close()
    return {row["issue_key"]: row["summary"] for row in rows}


def save_issue_scrum_teams(teams):
    if not teams:
        return
    now = time.time()
    conn = get_conn()
    for key, team in teams.items():
        if not key or not team:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO issue_scrum_teams (issue_key, scrum_team, updated_at) VALUES (?, ?, ?)",
            (key, team, now),
        )
    conn.commit()
    conn.close()


def get_issue_scrum_teams(keys=None):
    conn = get_conn()
    if keys is not None:
        keys = list(keys)
        if not keys:
            conn.close()
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            f"SELECT issue_key, scrum_team FROM issue_scrum_teams WHERE issue_key IN ({placeholders})",
            keys,
        ).fetchall()
    else:
        rows = conn.execute("SELECT issue_key, scrum_team FROM issue_scrum_teams").fetchall()
    conn.close()
    return {row["issue_key"]: row["scrum_team"] for row in rows}


def save_issue_labels(labels_by_key):
    if not labels_by_key:
        return
    now = time.time()
    conn = get_conn()
    for key, labels in labels_by_key.items():
        if not key:
            continue
        normalized_labels = sorted({str(label).strip() for label in (labels or []) if str(label).strip()})
        conn.execute(
            "INSERT OR REPLACE INTO issue_labels (issue_key, labels_json, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(normalized_labels), now),
        )
    conn.commit()
    conn.close()


def get_issue_labels(keys=None):
    conn = get_conn()
    if keys is not None:
        keys = list(keys)
        if not keys:
            conn.close()
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            f"SELECT issue_key, labels_json FROM issue_labels WHERE issue_key IN ({placeholders})",
            keys,
        ).fetchall()
    else:
        rows = conn.execute("SELECT issue_key, labels_json FROM issue_labels").fetchall()
    conn.close()

    result = {}
    for row in rows:
        try:
            result[row["issue_key"]] = json.loads(row["labels_json"] or "[]")
        except Exception:
            result[row["issue_key"]] = []
    return result


def save_issue_relationships(relationships):
    if not relationships:
        return
    now = time.time()
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_relationships (
            child_key TEXT NOT NULL,
            parent_key TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            updated_at REAL,
            PRIMARY KEY (child_key, parent_key, relationship_type)
        )
        """
    )
    for child_key, parent_key, relationship_type in relationships:
        if not child_key or not parent_key or not relationship_type:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO issue_relationships (child_key, parent_key, relationship_type, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (child_key, parent_key, relationship_type, now),
        )
    conn.commit()
    conn.close()


def get_issue_relationships(relationship_type=None, parent_keys=None, child_keys=None):
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_relationships (
            child_key TEXT NOT NULL,
            parent_key TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            updated_at REAL,
            PRIMARY KEY (child_key, parent_key, relationship_type)
        )
        """
    )
    clauses = []
    params = []

    if relationship_type:
        clauses.append("relationship_type = ?")
        params.append(relationship_type)

    if parent_keys is not None:
        parent_keys = [key for key in parent_keys if key]
        if not parent_keys:
            conn.close()
            return []
        placeholders = ",".join("?" * len(parent_keys))
        clauses.append(f"parent_key IN ({placeholders})")
        params.extend(parent_keys)

    if child_keys is not None:
        child_keys = [key for key in child_keys if key]
        if not child_keys:
            conn.close()
            return []
        placeholders = ",".join("?" * len(child_keys))
        clauses.append(f"child_key IN ({placeholders})")
        params.extend(child_keys)

    query = "SELECT child_key, parent_key, relationship_type, updated_at FROM issue_relationships"
    if clauses:
        query = f"{query} WHERE {' AND '.join(clauses)}"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def clear_all_scan_data():
    conn = get_conn()
    conn.execute("DELETE FROM checks")
    conn.execute("DELETE FROM field_findings")
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM issue_summaries")
    conn.execute("DELETE FROM issue_scrum_teams")
    conn.execute("DELETE FROM issue_labels")
    conn.execute("DELETE FROM issue_relationships")
    conn.commit()
    conn.close()


create_db()
