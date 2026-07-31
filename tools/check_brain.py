"""Can the brains run? Three probes, in the order that tells them apart.

Written 2026-07-31, when every brain run in the arena stopped with HTTP 403
"The caller does not have permission". The useful thing was not the 403 but
what sat either side of it: the same key answered `generateContent` normally,
and the agents list came back EMPTY. That combination says the credential is
alive and the entitlement is not, which is a different problem from a bad key
and a different problem again from a renamed agent.

Run it before and after any key change:

    set -a && . ./.env && set +a && python tools/check_brain.py

Exit code 0 means a brain can run right now.
"""

import os
import sys

import requests

BASE = "https://generativelanguage.googleapis.com/v1beta"
AGENT = "antigravity-preview-05-2026"   # keep in step with runner/brain.py


def probe(label, fn):
    try:
        r = fn()
    except Exception as e:  # a network failure is an answer too
        print(f"  {label:34} ERROR  {e}")
        return None
    body = r.text[:150].replace("\n", " ")
    print(f"  {label:34} {r.status_code}  {body}")
    return r


def main():
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        print("GEMINI_API_KEY is not set")
        return 2
    # The shape of the credential is itself a clue: an Antigravity key starts
    # with "AQ.", an ordinary Gemini API key with "AIza". They are not
    # interchangeable, and only the first can reach a managed agent.
    print(f"key: {key[:3]}… ({len(key)} chars)"
          + ("   [Antigravity]" if key.startswith("AQ.") else
             "   [ordinary Gemini key: cannot reach a managed agent]" if key.startswith("AIza")
             else "   [unrecognised shape]"))
    h = {"x-goog-api-key": key, "Content-Type": "application/json"}

    print("\n1. is the credential alive at all?")
    ordinary = probe("generateContent", lambda: requests.post(
        f"{BASE}/models/gemini-3.5-flash:generateContent", headers=h, timeout=60,
        json={"contents": [{"role": "user", "parts": [{"text": "say ok"}]}],
              "generationConfig": {"maxOutputTokens": 5}}))

    print("\n2. can this caller see the managed agent?")
    listed = probe("GET /agents", lambda: requests.get(f"{BASE}/agents", headers=h, timeout=30))
    probe(f"GET /agents/{AGENT[:18]}…", lambda: requests.get(f"{BASE}/agents/{AGENT}", headers=h, timeout=30))

    print("\n3. can it actually start an interaction?")
    started = probe("POST /interactions", lambda: requests.post(
        f"{BASE}/interactions", headers=h, timeout=90,
        json={"agent": AGENT, "input": [{"type": "text", "text": "say ok"}],
              "environment": {"type": "remote", "sources": [
                  {"type": "inline", "target": ".agents/AGENTS.md", "content": "# probe\n"}]}}))

    ok_ordinary = ordinary is not None and ordinary.status_code == 200
    agents = []
    if listed is not None and listed.status_code == 200:
        agents = (listed.json() or {}).get("agents") or []
    ok_start = started is not None and started.status_code < 300

    print("\nverdict:")
    if ok_start:
        print("  the brains can run.")
        return 0
    if ok_ordinary and not agents:
        print("  the credential works and this project can see NO managed agents.")
        print("  That is the entitlement, not the key: a new key from the same")
        print("  account without the entitlement will fail exactly the same way.")
    elif not ok_ordinary:
        print("  the credential itself is refused; start with the key.")
    else:
        print("  agents are visible but the interaction was refused; read the body above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
