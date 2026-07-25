"""First-bell plumbing: stage updates on the application doc.

The status card streams the application document, so each stage written here
is a line the principal watches appear in real time. Stages are written only
when the thing they name is actually happening — never ahead of it.

Usage:
  python -m jobs.bell APP_ID STAGE        # ringing | seating | first-session | done | failed
  python -m jobs.bell APP_ID --get-agent  # prints "<status>:<agent_id>" for the workflow
"""
import sys

from jobs.ingest import fs_client


def main():
    app_id, arg = sys.argv[1], sys.argv[2]
    fs = fs_client()
    ref = fs.collection("applications").document(app_id)
    if arg == "--get-agent":
        d = ref.get().to_dict() or {}
        print(f"{d.get('status', 'missing')}:{d.get('agent_id', '')}")
        return
    from google.cloud import firestore
    ref.update({"bell": {"stage": arg, "at": firestore.SERVER_TIMESTAMP}})
    print(f"bell({app_id}): {arg}")


main()
