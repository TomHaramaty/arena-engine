"""Antigravity (Gemini managed agent) brain client — raw REST, no SDK."""
import os
import time

import requests

BASE = "https://generativelanguage.googleapis.com/v1beta"
AGENT = "antigravity-preview-05-2026"
# Gemini 3.6 Flash paid-tier rates, USD per token
RATE_IN = 1.50 / 1e6
RATE_OUT = 7.50 / 1e6


class BrainError(Exception):
    pass


def _headers():
    return {
        "x-goog-api-key": os.environ["GEMINI_API_KEY"],
        "Content-Type": "application/json",
    }


def final_text(interaction):
    parts = []
    for step in interaction.get("steps", []):
        if step.get("type") == "model_output":
            parts = [c.get("text", "") for c in step.get("content", []) if c.get("text")]
    return "\n".join(parts)


def cost_usd(usage):
    tin = usage.get("total_input_tokens", 0)
    tout = usage.get("total_output_tokens", 0) + usage.get("total_thought_tokens", 0)
    return round(tin * RATE_IN + tout * RATE_OUT, 4)


def run(agents_md, task, timeout_s=900):
    """One interaction in a fresh sandbox with AGENTS.md mounted.
    Returns (text, usage, interaction_id)."""
    body = {
        "agent": AGENT,
        "input": [{"type": "text", "text": task}],
        "environment": {
            "type": "remote",
            "sources": [
                {"type": "inline", "target": ".agents/AGENTS.md", "content": agents_md}
            ],
        },
    }
    r = requests.post(
        f"{BASE}/interactions", headers=_headers(), json=body, timeout=timeout_s
    )
    if r.status_code != 200:
        raise BrainError(f"HTTP {r.status_code}: {r.text[:400]}")
    d = r.json()
    deadline = time.time() + timeout_s
    while d.get("status") in ("in_progress", "queued", "running"):
        if time.time() > deadline:
            raise BrainError(f"interaction {d.get('id')} timed out client-side")
        time.sleep(10)
        d = requests.get(
            f"{BASE}/interactions/{d['id']}", headers=_headers(), timeout=60
        ).json()
    if d.get("status") != "completed":
        raise BrainError(f"interaction ended with status {d.get('status')}: {str(d)[:400]}")
    text = final_text(d)
    if not text:
        raise BrainError("interaction completed but produced no model_output text")
    return text, d.get("usage", {}), d.get("id")
