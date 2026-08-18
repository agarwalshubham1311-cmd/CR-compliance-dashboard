"""
Field-level compliance rules — structurally different from compliance.py:
these check ONE entity's data quality, not alignment between two entities.

Field IDs confirmed against real TPOC-12 (CR) and TPOC-68 (Outcome) issues.
"""
import datetime

CR_FIELDS = {
    "epic_link": "customfield_10014",
    "complexity": "customfield_10109",
    "funded_by": "customfield_10106",
    "baseline_target_delivery_date": "customfield_10110",
    "baseline_prp_date": "customfield_10108",
    "actual_prp_start_date": "customfield_10107",
    "prp_status": "customfield_10111",
    "release_through": "customfield_10113",
    "target_delivery_date": "customfield_10112",
    "reason_for_delay": "customfield_10149",
    "blocked_by_dependency": "customfield_10150",
}

OUTCOME_FIELDS = {
    "target_delivery_date": "customfield_10112",
    "rag_status": "customfield_10114",
    "rag_outcome": "customfield_10115",
    "go_live_date": "customfield_10116",
}

# Statuses past which "overdue date" checks no longer apply — a delivered
# item with a target date in the past isn't overdue, it's just history.
TERMINAL_STATUSES_CR = {"DELIVERY COMPLETE"}
TERMINAL_STATUSES_OUTCOME = {"DONE", "LIVE - FEATURE SWITCHED ON", "LIVE - FEATURE SWITCHED OFF"}

# ON HOLD is treated as equivalent to BLOCKED for the blocked-reason check.
BLOCKED_ALIASES = {"BLOCKED", "ON HOLD"}


def _value(fields, field_id):
    """Normalize a raw Jira field value. Select-type custom fields come
    back as {'value': ...}; plain fields (dates, text) are strings."""
    raw = fields.get(field_id)
    if raw is None:
        return None
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    if isinstance(raw, str):
        return raw.strip() or None
    return raw


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def check_cr_fields(fields, status_name):
    """Returns a list of finding dicts: {check, field, severity, message}."""
    findings = []
    status_norm = (status_name or "").strip().upper()
    is_terminal = status_norm in TERMINAL_STATUSES_CR
    is_blocked = status_norm in BLOCKED_ALIASES

    required = [
        ("epic_link", "Epic Link"),
        ("complexity", "Complexity"),
        ("funded_by", "Funded By"),
        ("baseline_target_delivery_date", "Baseline Target Delivery Date"),
        ("baseline_prp_date", "Baseline PRP Date"),
        ("actual_prp_start_date", "Actual PRP Start Date"),
        ("prp_status", "PRP Status"),
        ("release_through", "Release Through"),
        ("target_delivery_date", "Target Delivery Date"),
    ]
    for key, label in required:
        if _value(fields, CR_FIELDS[key]) is None:
            findings.append({"check": "missing_field", "field": label, "severity": "Low",
                              "message": f"{label} is not set"})

    baseline = _value(fields, CR_FIELDS["baseline_target_delivery_date"])
    target = _value(fields, CR_FIELDS["target_delivery_date"])
    reason = _value(fields, CR_FIELDS["reason_for_delay"])

    if baseline and target and baseline != target and not reason:
        findings.append({"check": "delay_reason_missing", "field": "Reason for Delay", "severity": "Medium",
                          "message": "Target delivery date differs from baseline but no reason for delay is recorded"})
    if baseline and target and baseline == target and reason:
        findings.append({"check": "delay_reason_unexpected", "field": "Reason for Delay", "severity": "Low",
                          "message": "Reason for delay is filled in even though target matches baseline"})

    target_date = _parse_date(target)
    if target_date and not is_terminal and target_date < datetime.date.today():
        findings.append({"check": "overdue", "field": "Target Delivery Date", "severity": "High",
                          "message": f"Target delivery date ({target}) is in the past"})

    blocked_by = _value(fields, CR_FIELDS["blocked_by_dependency"])
    if is_blocked and not blocked_by:
        findings.append({"check": "blocked_reason_missing", "field": "Blocked by/Dependency", "severity": "Medium",
                          "message": "Status is Blocked/On Hold but Blocked by/Dependency is not set"})
    if not is_blocked and blocked_by:
        findings.append({"check": "blocked_reason_unexpected", "field": "Blocked by/Dependency", "severity": "Low",
                          "message": "Blocked by/Dependency is set but status is not Blocked/On Hold"})

    return findings


def check_outcome_fields(fields, status_name):
    findings = []
    status_norm = (status_name or "").strip().upper()
    is_terminal = status_norm in TERMINAL_STATUSES_OUTCOME

    required = [
        ("target_delivery_date", "Target Delivery Date"),
        ("rag_status", "RAG Status"),
        ("rag_outcome", "RAG Outcome"),
        ("go_live_date", "Go Live Date"),
    ]
    for key, label in required:
        if _value(fields, OUTCOME_FIELDS[key]) is None:
            findings.append({"check": "missing_field", "field": label, "severity": "Low",
                              "message": f"{label} is not set"})

    target = _value(fields, OUTCOME_FIELDS["target_delivery_date"])
    target_date = _parse_date(target)
    if target_date and not is_terminal and target_date < datetime.date.today():
        findings.append({"check": "overdue", "field": "Target Delivery Date", "severity": "High",
                          "message": f"Target delivery date ({target}) is in the past"})

    go_live = _value(fields, OUTCOME_FIELDS["go_live_date"])
    go_live_date = _parse_date(go_live)
    if go_live_date and not is_terminal and go_live_date < datetime.date.today():
        findings.append({"check": "go_live_overdue", "field": "Go Live Date", "severity": "High",
                          "message": f"Go Live date ({go_live}) is in the past"})

    return findings
