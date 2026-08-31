import bedrock_client


def draft_remediation_comment(target_key, target_status, target_desc, other_key, other_status, reason, severity):
    """Ask the LLM to draft a short Jira comment explaining a
    phase-alignment compliance issue (Story vs CR, Epic vs CR, Story vs
    Outcome — any two-entity comparison) and suggesting next steps.
    Returns plain text. Raises RuntimeError if Bedrock isn't reachable.

    target_key/target_status: the entity the comment will be posted on
    (the one that needs to act — e.g. the lagging Story, the Epic, the Outcome)
    other_key/other_status: the entity it's being compared against
    """

    prompt = f"""You are helping a project manager flag a Jira compliance issue in a comment.

Context:
- {target_key} is currently in status "{target_status}".
- Description: {target_desc or "(no description available)"}
- The linked item {other_key} is currently in status "{other_status}".
- Compliance issue detected: {reason}
- Severity: {severity}

Write a short, professional Jira comment (2-4 sentences) that:
1. States the mismatch factually (don't repeat the raw field names, write naturally)
2. Suggests one concrete next step or asks a clarifying question
3. Is addressed to whoever is assigned to {target_key}

Do not use markdown formatting. Output only the comment text, nothing else."""

    return bedrock_client.call_bedrock(prompt, max_tokens=200)


def draft_field_comment(entity_key, entity_status, findings):
    """Ask the LLM to draft a short Jira comment listing missing or
    invalid required fields and requesting they be completed.

    findings: list of dicts with at least 'field' and 'message' keys,
    as produced by field_rules.check_cr_fields / check_epic_fields /
    check_outcome_fields.
    """
    issues_text = "\n".join(f"- {f.get('field', 'Unknown field')}: {f.get('message', '')}" for f in findings)

    prompt = f"""You are helping a project manager flag missing required data on a Jira item in a comment.

Context:
- {entity_key} is currently in status "{entity_status}".
- The following required fields are missing or invalid:
{issues_text}

Write a short, professional Jira comment (2-4 sentences) that:
1. Lists what's missing in plain language (don't just repeat the field list verbatim)
2. Politely requests these be filled in
3. Is addressed to whoever is assigned to {entity_key}

Do not use markdown formatting. Output only the comment text, nothing else."""

    return bedrock_client.call_bedrock(prompt, max_tokens=200)
