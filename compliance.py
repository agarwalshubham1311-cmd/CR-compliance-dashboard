"""
Compliance rules derived from the ACC workflow diagrams (Change Request,
Story, Epic, Outcome).

Core idea: every status resolves onto a shared 0-7 canonical phase scale.
A "driver" entity has one status = one phase (CR, or Story acting as a
driver for Outcome). A "dependent" entity's status may legitimately span
several phases (a compliant window) — Story and Outcome as dependents,
and Epic (which reuses Story's phase table since its vocabulary is a
near-identical subset).

Directionality in this system:
  CR    drives Story    (Story should track its linked CR's phase)
  Story drives Outcome  (Outcome should track its linked Story's phase)
  CR    drives Epic     (Epic should track the SLOWEST of its linked CRs —
                          call evaluate_epic() with the full list of CR
                          statuses; it finds the bottleneck for you)

Blocked/Withdrawn (and On Hold, aliased to Blocked) are cross-cutting
states handled before any phase comparison.
"""

PHASES = [
    {  # phase 0 - pre-agreement / not started
        "cr": ["NEW REQUEST", "IMPACT ASSESSMENT REVIEW/APPROVAL", "IMPACT ASSESSMENT APPROVED",
               "IMPACT ASSESSMENT IN PROGRESS", "BUILD REVIEW / APPROVAL", "CR AGREED"],
        "story": ["BACKLOG"],
    },
    {  # phase 1
        "cr": ["STORIES CREATED"],
        "story": ["BACKLOG", "IN ANALYSIS"],
    },
    {  # phase 2
        "cr": ["IN DEVELOPMENT"],
        "story": ["IN ANALYSIS", "ANALYSIS DONE", "IN PROGRESS", "DEVELOPMENT DONE", "TEST (DEV)", "DEVTEST COMPLETE"],
    },
    {  # phase 3
        "cr": ["READY FOR MERGE"],
        "story": ["READY FOR MERGE"],
    },
    {  # phase 4
        "cr": ["IN FEATURE ENV"],
        "story": ["IN FEATURE ENVIRONMENT", "AUTOSIT"],
    },
    {  # phase 5
        "cr": ["IN PRP"],
        "story": ["READY FOR PRP TEST", "IN PRP TEST"],
    },
    {  # phase 6
        "cr": ["READY FOR PRODUCTION"],
        "story": ["READY FOR PROD"],
    },
    {  # phase 7
        "cr": ["DELIVERY COMPLETE"],
        "story": ["LIVE - FEATURE SWITCHED ON", "LIVE - FEATURE SWITCHED OFF"],
    },
]

# Outcome uses its own vocabulary (different from Story's), mapped onto the
# same 0-7 canonical scale from its own workflow diagram.
OUTCOME_PHASES = [
    {"outcome": ["TO DO", "DISCOVERY BACKLOG"]},                                     # phase 0
    {"outcome": ["IN ANALYSIS", "IN DESIGN", "IA IN PROGRESS",
                 "READY FOR DEVELOPMENT", "IN DEVELOPMENT", "INTEGRATION TESTING"]},  # phase 2
    {"outcome": ["READY FOR DEPLOYMENT"]},                                           # phase 3
    {"outcome": ["PRIVATE BETA"]},                                                   # phase 4
    {"outcome": ["PUBLIC BETA"]},                                                    # phase 5
    {"outcome": ["DONE"]},                                                           # phase 7
]
# Canonical scale index for each entry above (Outcome has fewer distinct
# stages than CR/Story, so this isn't a dense 0..5 run).
_OUTCOME_PHASE_INDEX = [0, 2, 3, 4, 5, 7]

CR_STATUS_TO_PHASE = {}
STORY_STATUS_TO_PHASES = {}
for idx, phase in enumerate(PHASES):
    for cr_status in phase["cr"]:
        CR_STATUS_TO_PHASE[cr_status] = idx
    for story_status in phase["story"]:
        STORY_STATUS_TO_PHASES.setdefault(story_status, set()).add(idx)

OUTCOME_STATUS_TO_PHASES = {}
for i, phase in enumerate(OUTCOME_PHASES):
    canonical_idx = _OUTCOME_PHASE_INDEX[i]
    for outcome_status in phase["outcome"]:
        OUTCOME_STATUS_TO_PHASES.setdefault(outcome_status, set()).add(canonical_idx)

# Category weights apply regardless of which entity pair is being compared —
# the reason STRING is built dynamically per pair type below, but severity
# weighting logic is shared across all pair types.
CATEGORY_WEIGHT = {
    "dependent_behind": 3,
    "dependent_withdrawn": 3,
    "driver_withdrawn": 3,
    "driver_blocked": 2,
    "dependent_blocked": 3,
    "dependent_ahead": 2,
    "unmapped": 1,
}


# Aliases map a raw Jira status name to the closest status this compliance
# table already knows about. Split by type since vocabularies differ —
# never use one type's alias dict for another type's status.
#
# Seeded with mappings confirmed by hand; new entries get added at runtime
# by ai_classify when the scanner encounters a status it hasn't seen
# before, so workflow changes don't require code edits.
CR_ALIASES = {}
STORY_ALIASES = {
    "TO DO": "BACKLOG",
    "DONE": "LIVE - FEATURE SWITCHED ON",
}
# Epic reuses the Story phase table (near-identical vocabulary) — this only
# needs to rename the handful of statuses that differ ("ANALYSIS" vs
# Story's "IN ANALYSIS"); everything else matches Story's names directly.
EPIC_ALIASES = {
    "ANALYSIS": "IN ANALYSIS",
}
OUTCOME_ALIASES = {
    "ON HOLD": "BLOCKED",
}

_ALIAS_TABLES = {"cr": CR_ALIASES, "story": STORY_ALIASES, "epic": EPIC_ALIASES, "outcome": OUTCOME_ALIASES}


def _norm(status, status_type):
    s = (status or "").strip().upper()
    return _ALIAS_TABLES[status_type].get(s, s)


def learn_alias(status_type, raw_status, target_status):
    """Record that raw_status should be treated as target_status for
    compliance purposes. Called after AI classification (or a human
    override) — takes effect immediately, and is expected to also be
    persisted to DB by the caller so it survives restarts."""
    key = (raw_status or "").strip().upper()
    target = (target_status or "").strip().upper()
    _ALIAS_TABLES[status_type][key] = target


def known_cr_statuses():
    return sorted(set(CR_STATUS_TO_PHASE.keys()) | {"BLOCKED", "WITHDRAWN"})


def known_story_statuses():
    return sorted(set(STORY_STATUS_TO_PHASES.keys()) | {"BLOCKED", "WITHDRAWN"})


def known_epic_statuses():
    # Epic uses Story's table (via alias), so its "known" set is the same
    # keys, since Epic's own vocabulary is a subset of Story's.
    return sorted(set(STORY_STATUS_TO_PHASES.keys()) | {"BLOCKED", "WITHDRAWN"})


def known_outcome_statuses():
    return sorted(set(OUTCOME_STATUS_TO_PHASES.keys()) | {"BLOCKED", "WITHDRAWN"})


def is_known(status_type, raw_status):
    """True if this status resolves to something in the phase table already
    (natively or via an existing alias) — false means it needs
    classification (AI or human) before compliance can be evaluated."""
    resolved = _norm(raw_status, status_type)
    if resolved in ("BLOCKED", "WITHDRAWN"):
        return True
    if status_type == "cr":
        return resolved in CR_STATUS_TO_PHASE
    if status_type in ("story", "epic"):
        return resolved in STORY_STATUS_TO_PHASES
    if status_type == "outcome":
        return resolved in OUTCOME_STATUS_TO_PHASES
    return False


def _driver_phase(status_type, norm_status):
    """Single-phase index for a status acting as the 'driver' side of a
    comparison. CR is naturally single-valued. Story, when driving an
    Outcome, uses the earliest phase its status could represent (the
    minimum of its compliance-window set) as its intrinsic position."""
    if status_type == "cr":
        return CR_STATUS_TO_PHASE.get(norm_status)
    if status_type == "story":
        phases = STORY_STATUS_TO_PHASES.get(norm_status)
        return min(phases) if phases else None
    return None


def _dependent_phases(status_type, norm_status):
    """Set of phase indices a status is compliant with, on the dependent
    side of a comparison."""
    if status_type == "story":
        return STORY_STATUS_TO_PHASES.get(norm_status)
    if status_type == "epic":
        return STORY_STATUS_TO_PHASES.get(norm_status)  # reused, see module docstring
    if status_type == "outcome":
        return OUTCOME_STATUS_TO_PHASES.get(norm_status)
    if status_type == "cr":
        phase = CR_STATUS_TO_PHASE.get(norm_status)
        return {phase} if phase is not None else None
    return None


def _score(category, age_days):
    weight = CATEGORY_WEIGHT.get(category, 1)
    score = weight * 10 + min(age_days, 10) * 2
    if score >= 45:
        severity = "Critical"
    elif score >= 35:
        severity = "High"
    elif score >= 22:
        severity = "Medium"
    else:
        severity = "Low"
    return score, severity


def evaluate_pair(driver_status, driver_type, dependent_status, dependent_type,
                   age_days=0, driver_label="CR", dependent_label="Story"):
    """Generic phase-alignment check between a driver and a dependent
    entity. Returns {compliant, reason, severity, score} — reason is None
    when compliant."""
    driver = _norm(driver_status, driver_type)
    dependent = _norm(dependent_status, dependent_type)

    if not driver or not dependent:
        score, severity = _score("unmapped", age_days)
        return {"compliant": False, "reason": "Unmapped status", "severity": severity, "score": score}

    if driver == "WITHDRAWN" and dependent != "WITHDRAWN":
        category, reason = "driver_withdrawn", f"{driver_label} withdrawn, {dependent_label} still active"
    elif dependent == "WITHDRAWN" and driver != "WITHDRAWN":
        category, reason = "dependent_withdrawn", f"{dependent_label} withdrawn, {driver_label} active"
    elif driver == "BLOCKED" and dependent != "BLOCKED":
        category, reason = "driver_blocked", f"{driver_label} blocked, {dependent_label} still progressing"
    elif dependent == "BLOCKED" and driver != "BLOCKED":
        category, reason = "dependent_blocked", f"{dependent_label} blocked, {driver_label} shows normal progress"
    elif driver in ("WITHDRAWN", "BLOCKED") or dependent in ("WITHDRAWN", "BLOCKED"):
        return {"compliant": True, "reason": None, "severity": None, "score": 0}
    else:
        driver_phase = _driver_phase(driver_type, driver)
        dependent_phases = _dependent_phases(dependent_type, dependent)

        if driver_phase is None or dependent_phases is None:
            category, reason = "unmapped", "Unmapped status"
        elif driver_phase in dependent_phases:
            return {"compliant": True, "reason": None, "severity": None, "score": 0}
        elif max(dependent_phases) < driver_phase:
            category, reason = "dependent_behind", f"{dependent_label} behind {driver_label} phase"
        elif min(dependent_phases) > driver_phase:
            category, reason = "dependent_ahead", f"{dependent_label} ahead of {driver_label} phase"
        else:
            category, reason = "unmapped", "Unmapped status"

    score, severity = _score(category, age_days)
    return {"compliant": False, "reason": reason, "severity": severity, "score": score}


def evaluate(cr_status, story_status, age_days=0):
    """CR -> Story phase alignment. Unchanged public signature/behavior
    from before this file was generalized — same reason strings as always."""
    return evaluate_pair(cr_status, "cr", story_status, "story", age_days,
                          driver_label="CR", dependent_label="Story")


def evaluate_outcome(story_status, outcome_status, age_days=0):
    """Story -> Outcome phase alignment."""
    return evaluate_pair(story_status, "story", outcome_status, "outcome", age_days,
                          driver_label="Story", dependent_label="Outcome")


def evaluate_epic(epic_status, cr_entries, age_days=0):
    """
    Epic <- CR phase alignment, where multiple CRs may link to one Epic.
    Per spec: the Epic should track the SLOWEST (lowest-phase) linked CR.

    cr_entries: list of (cr_key, cr_status) tuples — one per linked CR.
    Returns the same {compliant, reason, severity, score} shape, plus
    'bottleneck_cr_key' / 'bottleneck_cr_status' identifying which CR was
    the laggard (for dashboard drill-down).
    """
    if not cr_entries:
        return {"compliant": True, "reason": None, "severity": None, "score": 0,
                "bottleneck_cr_key": None, "bottleneck_cr_status": None}

    # Find the CR with the lowest phase (the bottleneck). Cross-cutting
    # statuses (Blocked/Withdrawn) don't have a numeric phase — treat a CR
    # in that state as the bottleneck by definition, since it isn't
    # progressing at all.
    worst_key, worst_status, worst_phase = None, None, None
    for cr_key, cr_status in cr_entries:
        norm = _norm(cr_status, "cr")
        if norm in ("BLOCKED", "WITHDRAWN"):
            worst_key, worst_status = cr_key, cr_status
            break
        phase = CR_STATUS_TO_PHASE.get(norm)
        if phase is None:
            continue
        if worst_phase is None or phase < worst_phase:
            worst_phase = phase
            worst_key, worst_status = cr_key, cr_status

    if worst_status is None:
        worst_key, worst_status = cr_entries[0]  # all unmapped — let evaluate_pair report it

    result = evaluate_pair(worst_status, "cr", epic_status, "epic", age_days,
                            driver_label="CR", dependent_label="Epic")
    result["bottleneck_cr_key"] = worst_key
    result["bottleneck_cr_status"] = worst_status
    return result
