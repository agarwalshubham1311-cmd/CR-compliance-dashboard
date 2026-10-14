import copy
import json
import os
from pathlib import Path

import field_rules


CONFIG_PATH = Path(
    os.environ.get(
        "LANDING_CONFIG_PATH",
        str(Path(__file__).with_name("landing_ui_config.json")),
    )
)


def _field_entries(field_labels, editable=True):
    return [
        {
            "key": key,
            "label": label,
            "visible": True,
            "required": True,
            "editable": editable,
        }
        for key, label in field_labels.items()
    ]


DEFAULT_CONFIG = {
    "rules": [
        {
            "key": "status_mismatches",
            "label": "Status mismatch with child",
            "description": "Detects when Outcome, Epic, CR or Story progression is not aligned with dependent work items.",
            "metric_key": "status_mismatches",
            "severity": "High",
            "enabled": True,
            "source": "scan",
        },
        {
            "key": "field_gaps",
            "label": "Field missing / invalid",
            "description": "Highlights mandatory or time-sensitive field gaps across Outcome, Epic and CR work items.",
            "metric_key": "field_gaps",
            "severity": "Medium",
            "enabled": True,
            "source": "scan",
        },
        {
            "key": "broken_hierarchy_links",
            "label": "Broken hierarchy link",
            "description": "Surfaces bottleneck misalignment across Outcome → Epic → CR relationships.",
            "metric_key": "broken_hierarchy_links",
            "severity": "High",
            "enabled": True,
            "source": "scan",
        },
        {
            "key": "pr_missing_against_cr",
            "label": "PR missing against CR",
            "description": "Reserved rule for PR / Slot evidence gaps against CRs. Scaffolded now so the UI does not need to change when SCM data is added.",
            "metric_key": "pr_missing_against_cr",
            "severity": "Low",
            "enabled": True,
            "source": "derived",
        },
    ],
    "fields": {
        "outcome": _field_entries(field_rules.OUTCOME_FIELD_LABELS, editable=True),
        "epic": _field_entries(field_rules.EPIC_FIELD_LABELS, editable=True),
        "cr": _field_entries(field_rules.CR_FIELD_LABELS, editable=True),
        "story": [
            {"key": "summary", "label": "Summary", "visible": True, "required": True, "editable": False},
            {"key": "status", "label": "Status", "visible": True, "required": True, "editable": False},
            {"key": "labels", "label": "Labels", "visible": True, "required": False, "editable": False},
            {"key": "assignee", "label": "Assignee", "visible": False, "required": False, "editable": False},
        ],
    },
    "agentic_ai": {
        "orchestrator": "LangGraph",
        "reasoning": "LangChain-style prompt orchestration",
        "retrieval": "RAG guidance for Jira governance and login support",
        "human_in_the_loop": True,
    },
    "views": {
        "default": "landing",
        "show_agentic_ai": True,
        "show_quick_actions": True,
        "show_configuration": True,
    },
}


def _merge_rules(saved_rules):
    saved_by_key = {rule.get("key"): rule for rule in (saved_rules or []) if rule.get("key")}
    merged = []
    for default_rule in DEFAULT_CONFIG["rules"]:
        merged_rule = copy.deepcopy(default_rule)
        merged_rule.update(saved_by_key.get(default_rule["key"], {}))
        merged.append(merged_rule)

    known_keys = {rule["key"] for rule in merged}
    for key, saved_rule in saved_by_key.items():
        if key not in known_keys:
            merged.append(saved_rule)
    return merged


def _merge_fields(saved_fields):
    merged = {}
    saved_fields = saved_fields or {}

    for entity_type, defaults in DEFAULT_CONFIG["fields"].items():
        saved_by_key = {
            field.get("key"): field
            for field in saved_fields.get(entity_type, [])
            if field.get("key")
        }
        entity_fields = []
        for default_field in defaults:
            merged_field = copy.deepcopy(default_field)
            merged_field.update(saved_by_key.get(default_field["key"], {}))
            entity_fields.append(merged_field)

        known_keys = {field["key"] for field in entity_fields}
        for key, saved_field in saved_by_key.items():
            if key not in known_keys:
                entity_fields.append(saved_field)
        merged[entity_type] = entity_fields

    for entity_type, fields in saved_fields.items():
        if entity_type not in merged:
            merged[entity_type] = fields

    return merged


def _merge_with_defaults(saved_config):
    saved_config = saved_config or {}
    merged = copy.deepcopy(DEFAULT_CONFIG)
    merged["rules"] = _merge_rules(saved_config.get("rules"))
    merged["fields"] = _merge_fields(saved_config.get("fields"))
    merged["agentic_ai"].update(saved_config.get("agentic_ai", {}))
    merged["views"].update(saved_config.get("views", {}))
    return merged


def load_landing_config():
    if not CONFIG_PATH.exists():
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except Exception:
        return copy.deepcopy(DEFAULT_CONFIG)

    return _merge_with_defaults(saved)


def save_landing_config(config_payload):
    merged = _merge_with_defaults(config_payload)
    CONFIG_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged

