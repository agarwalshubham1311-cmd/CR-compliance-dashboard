import json
import re
import bedrock_client


def classify_status(raw_status, status_type, known_statuses):
    """
    Ask the LLM which of the already-known statuses this new one is
    equivalent to, for compliance-phase purposes.

    known_statuses: list of status names already understood by compliance.py
    Returns: {"target": <one of known_statuses>, "confidence": "high"|"medium"|"low", "reasoning": str}
    Raises RuntimeError if Bedrock is unreachable or misconfigured.
    """
    label = "Change Request" if status_type == "cr" else "Story"
    options = "\n".join(f"- {s}" for s in known_statuses)

    prompt = f"""You are classifying Jira workflow statuses for a compliance system.

A new {label} status has appeared that isn't in our known list: "{raw_status}"

Here is the full list of statuses we already understand, in the SAME
{label} workflow:
{options}

Which one of these existing statuses represents the same workflow stage as
"{raw_status}"? Pick exactly one from the list above — do not invent a new
one, even if none seem like a perfect match; choose the closest.

Respond ONLY with JSON, no other text, in this exact shape:
{{"target": "<one of the statuses from the list, verbatim>", "confidence": "high|medium|low", "reasoning": "<one sentence>"}}"""

    content = bedrock_client.call_bedrock(prompt, max_tokens=150, temperature=0.1)

    # Models sometimes wrap JSON in markdown fences or add stray text —
    # extract the first {...} block defensively rather than trusting raw parse.
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError(f"Could not find JSON in model response: {content!r}")
    result = json.loads(match.group(0))

    if result.get("target") not in known_statuses:
        # Model picked something not in our list despite instructions —
        # fail closed rather than silently accepting a bad mapping.
        raise ValueError(f"Model returned unrecognized target status: {result.get('target')!r}")

    return result
