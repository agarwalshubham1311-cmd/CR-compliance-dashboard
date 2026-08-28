import sqlite3
import os
import time
import uuid

DB_PATH = os.environ.get("COMPLIANCE_DB_PATH", os.path.join(os.path.dirname(__file__), "compliance.db"))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            started_at REAL,
            crs_checked INTEGER,
            stories_checked INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            pair_type TEXT DEFAULT 'cr_story',
            cr_key TEXT,
            cr_status TEXT,
            cr_assignee TEXT,
            story_key TEXT,
            story_status TEXT,
            story_assignee TEXT,
            compliant INTEGER,
            reason TEXT,
            severity TEXT,
            score INTEGER,
            checked_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resolutions (
            story_key TEXT,
            cr_key TEXT,
            resolved_at REAL,
            PRIMARY KEY (story_key, cr_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS status_mappings (
            status_type TEXT,
            raw_status TEXT,
            target_status TEXT,
            source TEXT,
            reasoning TEXT,
            confidence TEXT,
            created_at REAL,
            reviewed INTEGER DEFAULT 0,
            PRIMARY KEY (status_type, raw_status)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS field_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            entity_type TEXT,
            entity_key TEXT,
            entity_status TEXT,
            check_name TEXT,
            field TEXT,
            severity TEXT,
            message TEXT,
            checked_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_field_findings_run ON field_findings(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_run ON checks(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checks_story_cr ON checks(story_key, cr_key)")

    # Migration: existing databases created before pair_type existed won't
    # have the column even after CREATE TABLE IF NOT EXISTS (that only
    # applies to brand-new tables). Add it if missing, defaulting existing
    # rows to 'cr_story' since that's the only pair type that existed then.
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(checks)").fetchall()]
    if "pair_type" not in existing_cols:
        conn.execute("ALTER TABLE checks ADD COLUMN pair_type TEXT DEFAULT 'cr_story'")
    if "cr_assignee" not in existing_cols:
        conn.execute("ALTER TABLE checks ADD COLUMN cr_assignee TEXT")
    if "story_assignee" not in existing_cols:
        conn.execute("ALTER TABLE checks ADD COLUMN story_assignee TEXT")

    conn.commit()
    conn.close()


def save_run(results):
    """results: list of dicts with cr_key, cr_status, story_key, story_status,
    compliant, reason, severity, score, and optionally pair_type (defaults
    to 'cr_story' if omitted, for backward compatibility). For 'epic_cr'
    rows, cr_key/cr_status hold the bottleneck CR's info and story_key/
    story_status hold the Epic's info. For 'story_outcome' rows, cr_key/
    cr_status hold the Story's info and story_key/story_status hold the
    Outcome's info — reusing the same columns keeps the schema simple;
    pair_type is what disambiguates which entity is which.
    Writes one run + all check rows."""
    run_id = str(uuid.uuid4())
    now = time.time()
    conn = get_conn()
    crs = set(r["cr_key"] for r in results)
    stories = set(r["story_key"] for r in results)
    conn.execute(
        "INSERT INTO runs (run_id, started_at, crs_checked, stories_checked) VALUES (?, ?, ?, ?)",
        (run_id, now, len(crs), len(stories)),
    )
    for r in results:
        conn.execute(
            """INSERT INTO checks (run_id, pair_type, cr_key, cr_status, cr_assignee, story_key, story_status,
               story_assignee, compliant, reason, severity, score, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, r.get("pair_type", "cr_story"), r["cr_key"], r["cr_status"], r.get("cr_assignee"),
             r["story_key"], r["story_status"], r.get("story_assignee"), int(r["compliant"]),
             r.get("reason"), r.get("severity"), r.get("score", 0), now),
        )
    conn.commit()
    conn.close()
    return run_id


def save_additional_checks(run_id, results, pair_type):
    """Append Epic-CR or Story-Outcome findings under an EXISTING run_id
    (from the same scan pass's save_run() call) — does NOT touch the runs
    table, so it doesn't affect crs_checked/stories_checked counts that
    the existing CR-Story dashboard relies on. Each result dict uses the
    same cr_key/cr_status/story_key/story_status field names as save_run
    (see save_run's docstring for what they mean per pair_type)."""
    if not results:
        return
    now = time.time()
    conn = get_conn()
    for r in results:
        conn.execute(
            """INSERT INTO checks (run_id, pair_type, cr_key, cr_status, cr_assignee, story_key, story_status,
               story_assignee, compliant, reason, severity, score, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, pair_type, r["cr_key"], r["cr_status"], r.get("cr_assignee"), r["story_key"],
             r["story_status"], r.get("story_assignee"), int(r["compliant"]), r.get("reason"),
             r.get("severity"), r.get("score", 0), now),
        )
    conn.commit()
    conn.close()


def get_pair_findings(run_id, pair_type):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM checks WHERE run_id = ? AND pair_type = ? AND compliant = 0", (run_id, pair_type)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_run_id():
    conn = get_conn()
    row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    conn.close()
    return row["run_id"] if row else None


def get_resolved_keys():
    conn = get_conn()
    rows = conn.execute("SELECT story_key, cr_key FROM resolutions").fetchall()
    conn.close()
    return set((r["story_key"], r["cr_key"]) for r in rows)


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
        """INSERT OR REPLACE INTO status_mappings
           (status_type, raw_status, target_status, source, reasoning, confidence, created_at, reviewed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (status_type, raw_status.upper(), target_status.upper(), source, reasoning, confidence,
         time.time(), 1 if source == "human" else 0),
    )
    conn.commit()
    conn.close()


def save_field_findings(run_id, entity_type, entity_key, entity_status, findings):
    """findings: list of {check, field, severity, message} dicts, as
    produced by field_rules.check_cr_fields / check_outcome_fields."""
    if not findings:
        return
    now = time.time()
    conn = get_conn()
    for f in findings:
        conn.execute(
            """INSERT INTO field_findings
               (run_id, entity_type, entity_key, entity_status, check_name, field, severity, message, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, entity_type, entity_key, entity_status, f["check"], f.get("field"),
             f["severity"], f["message"], now),
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
            "SELECT * FROM field_findings WHERE run_id = ? AND entity_type = ?", (run_id, entity_type)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM field_findings WHERE run_id = ?", (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_status_mappings():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM status_mappings").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unreviewed_status_mappings():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM status_mappings WHERE reviewed = 0").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_status_mapping_reviewed(status_type, raw_status):
    conn = get_conn()
    conn.execute(
        "UPDATE status_mappings SET reviewed = 1 WHERE status_type = ? AND raw_status = ?",
        (status_type, raw_status.upper()),
    )
    conn.commit()
    conn.close()


def get_latest_summary():
    run_id = latest_run_id()
    if not run_id:
        return {"run_id": None, "crs_checked": 0, "stories_checked": 0, "non_compliant": 0,
                "compliance_rate": None, "active": []}

    conn = get_conn()
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    # Filter to pair_type = 'cr_story' explicitly — Epic-CR and Story-Outcome
    # rows share the same run_id (see save_additional_checks) but must not
    # leak into this summary, which the existing dashboard assumes is
    # CR-Story data only.
    checks = conn.execute(
        "SELECT * FROM checks WHERE run_id = ? AND pair_type = 'cr_story'", (run_id,)
    ).fetchall()
    conn.close()

    resolved = get_resolved_keys()
    active_non_compliant = [
        dict(c) for c in checks
        if not c["compliant"] and (c["story_key"], c["cr_key"]) not in resolved
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
    """Compliance rate per run, most recent `days` runs (or all if fewer).
    One point per run — schedule runs however often you want granularity."""
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
            (run["run_id"],)
        ).fetchall()
        conn.close()
        non_compliant = sum(
            1 for c in checks if not c["compliant"] and (c["story_key"], c["cr_key"]) not in resolved
        )
        total = run["stories_checked"] or 1
        rate = round(((total - non_compliant) / total) * 100)
        trend.append({"run_id": run["run_id"], "checked_at": run["started_at"], "compliance_rate": rate})
    return trend
