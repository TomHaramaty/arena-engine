"""Antigravity (Gemini managed agent) brain client — raw REST, no SDK."""
import os
import time

import requests

BASE = "https://generativelanguage.googleapis.com/v1beta"
AGENT = "antigravity-preview-05-2026"
# Paid-tier rates for the Antigravity agent tier above, USD per token —
# these feed the public "brain spend" figures; update them if AGENT changes.
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


# A brain run is not idempotent and is never auto-retried by the dispatcher: an
# agent whose session dies loses that market slot, and the loss is invisible
# except as a row stuck in 'started'. Measured on the record 2026-07-30: 7 of
# 139 runs (5%) died that way, every one of them with zero operations applied —
# i.e. in this file, at the model call — and one of the seven was a real
# principal's first bell. So transient failures are retried HERE, inside the
# run, where a retry is still honest: nothing has been applied, nothing has
# been journalled, and asking the same question again is not repairing an
# answer.
TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
# A poll that fails says nothing about the interaction, which is running on
# someone else's machine. Only a run of consecutive failures means we have
# genuinely lost contact with it.
POLL_FAILURES_ALLOWED = 6


def _post_interaction(body, timeout_s, attempts=3):
    """Start an interaction, retrying transient refusals with backoff.

    A retry after a lost response can start a second interaction server-side.
    That costs money and nothing else: the operations that reach the record come
    from the text WE read back, so an orphaned interaction is never applied.
    """
    delay = 5.0
    last = None
    for attempt in range(attempts):
        try:
            r = requests.post(
                f"{BASE}/interactions", headers=_headers(), json=body,
                timeout=timeout_s,
            )
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:400]}"
            if r.status_code not in TRANSIENT_STATUS:
                raise BrainError(last)
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        except ValueError as e:                     # 200 with unreadable body
            last = f"unreadable response: {e}"
        if attempt == attempts - 1:
            break
        print(f"  brain start failed ({last}) — retrying in {delay:.0f}s")
        time.sleep(delay)
        delay *= 3
    raise BrainError(f"could not start interaction after {attempts} attempts: {last}")


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
    d = _post_interaction(body, timeout_s)
    deadline = time.time() + timeout_s
    misses = 0
    while d.get("status") in ("in_progress", "queued", "running"):
        if time.time() > deadline:
            raise BrainError(f"interaction {d.get('id')} timed out client-side")
        time.sleep(10)
        try:
            r = requests.get(
                f"{BASE}/interactions/{d['id']}", headers=_headers(), timeout=60
            )
            r.raise_for_status()
            d, misses = r.json(), 0
        except (requests.RequestException, ValueError) as e:
            misses += 1
            if misses > POLL_FAILURES_ALLOWED:
                raise BrainError(
                    f"lost contact with interaction {d.get('id')} after "
                    f"{misses} consecutive failed polls: {e}"
                )
            print(f"  poll {misses}/{POLL_FAILURES_ALLOWED} failed ({e}) — retrying")
    if d.get("status") != "completed":
        raise BrainError(f"interaction ended with status {d.get('status')}: {str(d)[:400]}")
    text = final_text(d)
    if not text:
        raise BrainError("interaction completed but produced no model_output text")
    return text, d.get("usage", {}), d.get("id")
