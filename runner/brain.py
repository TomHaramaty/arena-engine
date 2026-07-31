"""Gemini Interactions API brain client — raw REST, no SDK.

TWO TARGETS, ONE PROTOCOL. The Interactions API can address either a managed
agent (which brings a sandboxed Linux environment with tools and file mounts)
or a model directly. Everything else — starting an interaction, polling it,
reading `model_output`, the usage block — is identical, which is why the
switch below is four lines rather than a rewrite.

The arena ran on the managed agent `antigravity-preview-05-2026` from launch
until 2026-07-31 14:03 UTC, when starting an interaction with it began
returning 403 "The caller does not have permission" for this project. Measured
that day, in this order, because each probe rules something out:

  - the same key answers generateContent 200            (the credential lives)
  - a fresh key from a SECOND Google account: same 403  (not the credential)
  - a model-targeted interaction, same key: 200         (not the API, not us)

So the model path is the one that works, and it is now the default. The agent
path is kept behind BRAIN_AGENT because the sandbox is genuinely richer and
the entitlement may come back: set it to the agent name to go back.

WHAT MOVED. The agent read its rulebook from a mounted `.agents/AGENTS.md`;
a model reads the same text as its system instruction. The agent brought its
own web access; the model is handed `google_search` explicitly, because
runner/context.py tells every brain to "Research with google_search" and a
brain that cannot search would quietly stop doing the research its charter
promises. What is NOT replaced: code execution and file management. No part
of a session used them — a brain deliberates and emits typed operations as
text — but a future prompt must not assume them.
"""
import os
import time

import requests

BASE = "https://generativelanguage.googleapis.com/v1beta"

#: Set to a managed agent name (e.g. "antigravity-preview-05-2026") to run
#: sessions in a sandbox again. Empty means the model path below.
AGENT = os.environ.get("BRAIN_AGENT", "")

#: The model behind every brain. gemini-3.6-flash is priced at exactly the
#: rates the Antigravity agent tier was billed at ($1.50 / $7.50 per 1M), which
#: is the best evidence available that it is the same class of model the agent
#: was running underneath: the arena's recorded spend stays comparable across
#: the change rather than silently re-basing. BRAIN_MODEL overrides it without
#: a deploy.
MODEL = os.environ.get("BRAIN_MODEL", "gemini-3.6-flash")

#: Paid-tier rates, USD per token, per model. These feed the recorded brain
#: spend, so a model change that forgets its rates makes the record lie.
#: Thought tokens bill as output (see cost_usd).
RATES = {
    "gemini-3.6-flash": (1.50 / 1e6, 7.50 / 1e6),
    "gemini-3.5-flash": (1.50 / 1e6, 9.00 / 1e6),
    "gemini-3.1-pro-preview": (2.00 / 1e6, 12.00 / 1e6),
    "gemini-2.5-pro": (1.25 / 1e6, 10.00 / 1e6),
}
RATE_IN, RATE_OUT = RATES.get(MODEL, (1.50 / 1e6, 7.50 / 1e6))


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


def request_body(agents_md, task):
    """The interaction to start: a model carrying the rulebook as its system
    instruction, or the managed agent with the rulebook mounted as a file.

    Pure, so the shape of what is asked can be asserted without a network.
    """
    if AGENT:
        return {
            "agent": AGENT,
            "input": [{"type": "text", "text": task}],
            "environment": {
                "type": "remote",
                "sources": [
                    {"type": "inline", "target": ".agents/AGENTS.md", "content": agents_md}
                ],
            },
        }
    return {
        "model": MODEL,
        # the same words the agent read off a mounted file
        "system_instruction": agents_md,
        "input": [{"type": "text", "text": task}],
        # the sandbox's web access, asked for by name
        "tools": [{"type": "google_search"}],
    }


def run(agents_md, task, timeout_s=900):
    """One interaction carrying this agent's rulebook and today's task.
    Returns (text, usage, interaction_id)."""
    d = _post_interaction(request_body(agents_md, task), timeout_s)
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
