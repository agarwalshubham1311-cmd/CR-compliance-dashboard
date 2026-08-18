import os
import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1/chat/completions")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")


def draft_remediation_comment(story_key, story_status, story_desc, cr_key, cr_status, reason, severity):
    """Ask the local LLM to draft a short Jira comment explaining the
    compliance mismatch and suggesting next steps. Returns plain text.
    Raises requests.RequestException if Ollama isn't reachable."""

    prompt = f"""You are helping a project manager flag a Jira compliance issue in a comment.

Context:
- Story {story_key} is currently in status "{story_status}".
- Story description: {story_desc or "(no description available)"}
- Its linked Change Request {cr_key} is currently in status "{cr_status}".
- Compliance issue detected: {reason}
- Severity: {severity}

Write a short, professional Jira comment (2-4 sentences) that:
1. States the mismatch factually (don't repeat the raw field names, write naturally)
2. Suggests one concrete next step or asks a clarifying question
3. Is addressed to whoever is assigned to the story

Do not use markdown formatting. Output only the comment text, nothing else."""

    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 200,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()
