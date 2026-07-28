"""First-bell plumbing: stage updates on the application doc.

The status card streams the application document, so each stage written here
is a line the principal watches appear in real time. Stages are written only
when the thing they name is actually happening — never ahead of it.

Usage:
  python -m jobs.bell APP_ID STAGE        # ringing | seating | first-session | publishing | done | failed
  python -m jobs.bell APP_ID --get-agent  # prints "<status>:<agent_id>" for the workflow
  python -m jobs.bell APP_ID --await-floor AGENT_ID  # block until the floor serves AGENT_ID
"""
import os
import sys
import time

import requests

from jobs.ingest import fs_client
from engine import observability as obs

FLOOR_URL = os.environ.get("FLOOR_URL", "https://open-outcry.web.app/arena.json")
FLOOR_WAIT_SECONDS = int(os.environ.get("FLOOR_WAIT_SECONDS", "240"))
FLOOR_POLL_SECONDS = int(os.environ.get("FLOOR_POLL_SECONDS", "5"))


def floor_has(agent_id):
    """True once the deployed floor actually serves this agent."""
    r = requests.get(FLOOR_URL, timeout=15, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    return any((a.get("id") or "").lower() == agent_id.lower()
               for a in r.json().get("agents", []))


def await_floor(agent_id):
    """Block until the published floor carries the agent.

    Pushing arena.json only lands the data in arena-web's repo; the floor is a
    build artifact, so the trader is not on it until arena-web's deploy has
    run — about a minute later. The status card offers "watch it on the floor"
    the instant the done stage is written, so this is the only honest moment
    to write it.

    Never fails the run: a slow deploy is not a failed bell, and the entry is
    on the record either way.
    """
    started = time.monotonic()
    while True:
        try:
            if floor_has(agent_id):
                print(f"floor serves {agent_id} after {time.monotonic() - started:.0f}s")
                return True
        except Exception as e:
            print(f"floor check: {e}")
        if time.monotonic() - started >= FLOOR_WAIT_SECONDS:
            print(f"floor still missing {agent_id} after {FLOOR_WAIT_SECONDS}s — "
                  "going on; the next tick publishes it")
            return False
        time.sleep(FLOOR_POLL_SECONDS)


def main():
    obs.init("bell")
    app_id, arg = sys.argv[1], sys.argv[2]
    if arg == "--await-floor":
        await_floor(sys.argv[3])
        return
    fs = fs_client()
    ref = fs.collection("applications").document(app_id)
    if arg == "--get-agent":
        d = ref.get().to_dict() or {}
        print(f"{d.get('status', 'missing')}:{d.get('agent_id', '')}")
        return
    from google.cloud import firestore
    ref.update({"bell": {"stage": arg, "at": firestore.SERVER_TIMESTAMP}})
    print(f"bell({app_id}): {arg}")


if __name__ == "__main__":
    main()
