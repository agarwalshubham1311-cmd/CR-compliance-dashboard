import os
import json
import re
import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1/chat/completions")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")


def classify_status(raw_status, status_type, known_statuses):
    """
    Ask the local LLM which of the already-known statuses this new one is
    equivalent to, for compliance-phase purposes.

    known_statuses: list of status names already understood by compliance.py
    Returns: {"target": <one of known_statuses>, "confidence": "high"|"medium"|"low", "reasoning": str}
    Raises requests.RequestException if Ollama is unreachable.
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

    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 150,
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()

    # Local models sometimes wrap JSON in markdown fences or add stray text —
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
