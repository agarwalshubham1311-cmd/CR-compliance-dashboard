"""
Field-level compliance rules — structurally different from compliance.py:
these check ONE entity's data quality, not alignment between two entities.

Field IDs confirmed against real TPOC-12 (CR), TPOC-44 (Epic), TPOC-68
(Outcome) issues.
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

EPIC_FIELDS = {
    "due_date": "duedate",
    "target_delivery_date": "customfield_10184",  # Epic's OWN field — distinct from CR/Outcome's customfield_10112
    "description": "description",
    "labels": "labels",
    "priority": "priority",
    "pi": "customfield_10183",
    "delivery_data_confidence": "customfield_10185",
}

# Human-readable labels for each field key, used by the /api/entities/*
# endpoint and the editable-fields UI.
CR_FIELD_LABELS = {
    "epic_link": "Epic Link", "complexity": "Complexity", "funded_by": "Funded By",
    "baseline_target_delivery_date": "Baseline Target Delivery Date",
    "baseline_prp_date": "Baseline PRP Date", "actual_prp_start_date": "Actual PRP Start Date",
    "prp_status": "PRP Status", "release_through": "Release Through",
    "target_delivery_date": "Target Delivery Date", "reason_for_delay": "Reason for Delay",
    "blocked_by_dependency": "Blocked by/Dependency",
}
EPIC_FIELD_LABELS = {
    "due_date": "Due Date", "target_delivery_date": "Target Delivery Date",
    "description": "Description", "labels": "Labels", "priority": "Priority",
    "pi": "PI", "delivery_data_confidence": "Delivery Data Confidence",
}
OUTCOME_FIELD_LABELS = {
    "target_delivery_date": "Target Delivery Date", "rag_status": "RAG Status",
    "rag_outcome": "RAG Outcome", "go_live_date": "Go Live Date",
}

# Field types, confirmed from real raw Jira data where a value was seen
# populated (Complexity, PRP Status, Release Through, Priority — all
# select-type with {"value": ...} shape). Fields never seen populated
# (PI, Delivery Data Confidence, RAG Status, RAG Outcome) default to
# "text" — if a write is rejected, the error reveals the real type.
CR_FIELD_TYPES = {
    "epic_link": "text", "complexity": "select", "funded_by": "text",
    "baseline_target_delivery_date": "date", "baseline_prp_date": "date",
    "actual_prp_start_date": "date", "prp_status": "select", "release_through": "select",
    "target_delivery_date": "date", "reason_for_delay": "text", "blocked_by_dependency": "text",
}
EPIC_FIELD_TYPES = {
    "due_date": "date", "target_delivery_date": "date", "description": "text",
    "labels": "labels", "priority": "select",
    "pi": "text",                        # unconfirmed — never seen populated
    "delivery_data_confidence": "text",  # unconfirmed — never seen populated
}
OUTCOME_FIELD_TYPES = {
    "target_delivery_date": "date",
    "rag_status": "text",   # unconfirmed — never seen populated (likely select)
    "rag_outcome": "text",  # unconfirmed — never seen populated
    "go_live_date": "date",
}

# Options for confirmed select-type fields, taken from real values observed.
# Verify against Project Settings -> Fields if any option is wrong.
SELECT_OPTIONS = {
    "complexity": ["Low", "Medium", "High"],
    "prp_status": ["Not Started", "In Progress", "Complete"],
    "release_through": ["Standard Release", "Hotfix"],
    "priority": ["Highest", "High", "Medium", "Low", "Lowest"],
}


def format_field_value(field_key, field_type, raw_value):
    """Shapes a plain value (as typed by a user) into what Jira's REST API
    expects for that field type."""
    if raw_value is None or raw_value == "":
        return None
    if field_type == "select":
        return {"value": raw_value} if field_key != "priority" else {"name": raw_value}
    if field_type == "labels":
        if isinstance(raw_value, list):
            return raw_value
        return [v.strip() for v in raw_value.split(",") if v.strip()]
    return raw_value  # text, date — Jira accepts these as plain strings


# Statuses past which "overdue date" checks no longer apply - a delivered
# item with a target date in the past isn't overdue, it's just history.
TERMINAL_STATUSES_CR = {"DELIVERY COMPLETE"}
TERMINAL_STATUSES_OUTCOME = {"DONE", "LIVE - FEATURE SWITCHED ON", "LIVE - FEATURE SWITCHED OFF"}

# ON HOLD is treated as equivalent to BLOCKED for the blocked-reason check.
BLOCKED_ALIASES = {"BLOCKED", "ON HOLD"}


def _adf_to_text(node):
    """Extract plain text from an Atlassian Document Format (ADF) value —
    used by rich-text custom fields. Description was already known to be
    ADF; "Reason for Delay" and "Blocked by/Dependency" turned out to be
    ADF too (not plain text as originally assumed), which is what caused
    them to display as "[object Object]" before this fix."""
    if not isinstance(node, dict):
        return None
    texts = []

    def _walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text" and n.get("text"):
                texts.append(n["text"])
            for child in n.get("content", []):
                _walk(child)
        elif isinstance(n, list):
            for item in n:
                _walk(item)

    _walk(node)
    joined = " ".join(texts).strip()
    return joined or None


def _value(fields, field_id):
    """Normalize a raw Jira field value. Select-type custom fields come
    back as {'value': ...}; rich-text fields come back as ADF (nested
    dict with type/content); plain fields (dates, text) are strings.
    Any other unrecognized object shape returns None rather than leaking
    the raw dict to a caller expecting a displayable value."""
    raw = fields.get(field_id)
    if raw is None:
        return None
    if isinstance(raw, dict):
        if "value" in raw:
            return raw["value"]
        if raw.get("type") == "doc":
            return _adf_to_text(raw)
        return None  # unrecognized object shape — don't leak it as a display value
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


def _description_text(raw):
    """Kept as a thin alias — same ADF-parsing logic as _adf_to_text,
    used specifically for Epic's description field."""
    return _adf_to_text(raw)


def check_epic_fields(fields, status_name):
    findings = []

    due_date = _value(fields, EPIC_FIELDS["due_date"])
    if due_date is None:
        findings.append({"check": "missing_field", "field": "Due Date", "severity": "Low",
                          "message": "Due Date is not set"})

    target = _value(fields, EPIC_FIELDS["target_delivery_date"])
    if target is None:
        findings.append({"check": "missing_field", "field": "Target Delivery Date", "severity": "Low",
                          "message": "Target Delivery Date is not set"})

    desc_text = _description_text(fields.get(EPIC_FIELDS["description"]))
    if not desc_text:
        findings.append({"check": "missing_field", "field": "Description", "severity": "Low",
                          "message": "Description is empty"})

    labels = fields.get(EPIC_FIELDS["labels"])
    if not labels:
        findings.append({"check": "missing_field", "field": "Labels", "severity": "Low",
                          "message": "No labels set"})

    priority = _value(fields, EPIC_FIELDS["priority"])
    if priority is None:
        findings.append({"check": "missing_field", "field": "Priority", "severity": "Low",
                          "message": "Priority is not set"})

    pi = _value(fields, EPIC_FIELDS["pi"])
    if pi is None:
        findings.append({"check": "missing_field", "field": "PI", "severity": "Low",
                          "message": "PI is not set"})

    delivery_confidence = _value(fields, EPIC_FIELDS["delivery_data_confidence"])
    if delivery_confidence is None:
        findings.append({"check": "missing_field", "field": "Delivery Data Confidence", "severity": "Low",
                          "message": "Delivery Data Confidence is not set"})

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
