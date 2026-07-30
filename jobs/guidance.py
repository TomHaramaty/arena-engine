"""The desk's one channel into the record: guidance a principal files.

Two directions, both run from the tick (and again right after a daily run, so
an answer reaches the desk while the session is still warm):

  in   Firestore `guidance` docs with status='filed' → verified against the
       agent's owner → C<n> → a row, an entry in the trader's own guidance file,
       and the doc flipped to 'ingested'. From there `runner/context` puts it in
       front of the brain at its next session.
  out  guidance the agent has answered → the disposition and its words written
       back to the doc, and appended to the record file as a second entry.

Nothing here is editable after the fact: an answer is a new entry under the same
C-id, never a rewrite of the note.

Usage:
  python -m jobs.guidance           # both directions
  python -m jobs.guidance --check   # read-only: what is waiting, what is answered
"""
import os
import pathlib
import sys
import traceback
from datetime import datetime, timezone

from engine import db, gitrepo
from engine import observability as obs
from jobs.ingest import fs_client

TRADER = pathlib.Path(os.environ.get("TRADER_REPO", "/Users/tomharamaty/trader"))
MAX_TEXT = 4000
PER_DAY = 3  # notes one principal may file for one trader in a day

HEADER = ("# {name} — guidance\n\n"
          "Notes {name} carried from its desk: the principal's own words, or "
          "{name}'s note of what a conversation with them settled. Guidance has "
          "standing and no authority: {name} must answer every note at its next "
          "session, and may decline or refuse it with reasons. Entries are "
          "appended, never rewritten; an answer is a new entry under the same "
          "id.\n")

DISPOSITIONS = ("adopted", "converted", "declined", "refused")


def filed_docs(fs):
    from google.cloud.firestore_v1.base_query import FieldFilter
    return list(fs.collection("guidance").where(
        filter=FieldFilter("status", "==", "filed")).stream())


def guidance_path(agent_id):
    return TRADER / "agents" / agent_id / "guidance.md"


def append_entry(agent_id, name, heading, body):
    """Append one block to the trader's guidance file, creating it with its
    header the first time. Returns the path touched."""
    path = guidance_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else HEADER.format(name=name)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(f"{text}\n## {heading}\n{body.rstrip()}\n", encoding="utf-8")
    return path


def commit(paths, message):
    """CI only: the workflows clone a fresh trader repo and hold its SSH key.
    Local runs leave the working tree for the operator to read."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not paths:
        print(f"  local run — {len(paths)} path(s) left uncommitted")
        return
    gitrepo.commit_and_push(TRADER, paths, message)


def reject(doc, reason):
    from google.cloud import firestore
    doc.reference.update({"status": "rejected", "reason": reason,
                          "rejectedAt": firestore.SERVER_TIMESTAMP})
    print(f"  {doc.id}: NOT FILED — {reason}")


def take(conn, doc, today):
    """One filed note → the record. Returns the path touched, or None."""
    from google.cloud import firestore
    data = doc.to_dict() or {}
    uid = str(data.get("uid") or "")
    aid = str(data.get("trader") or "")
    text = str(data.get("text") or "").strip()
    author = "trader" if data.get("author") == "trader" else "principal"

    if not text:
        return reject(doc, "The note was empty.")
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip()

    row = conn.execute(
        "select id, name, owner_uid, status from agents where id=%s", (aid,)).fetchone()
    # ownership is proved here, never taken from the client
    if not row or row["owner_uid"] != uid or not uid:
        return reject(doc, "That trader does not answer to this account.")
    if row["status"] != "active":
        return reject(doc, f"{row['name']} is no longer taking guidance.")

    already = conn.execute(
        "select count(*) c from guidance where agent_id=%s and filed_at::date=%s",
        (aid, today)).fetchone()["c"]
    if already >= PER_DAY:
        return reject(doc, f"{row['name']} takes {PER_DAY} notes a day; this one "
                           "was not filed. Try again tomorrow.")

    n = conn.execute("select count(*) c from guidance where agent_id=%s",
                     (aid,)).fetchone()["c"]
    cid = f"C{n + 1}"
    conn.execute(
        """insert into guidance (agent_id, cid, uid, doc_id, text, author)
           values (%s,%s,%s,%s,%s,%s) on conflict (doc_id) do nothing""",
        (aid, cid, uid, doc.id, text, author))
    conn.commit()

    whose = (f"— {row['name']}'s own note, from the desk" if author == "trader"
             else "— the principal's words, from the desk")
    path = append_entry(
        aid, row["name"], f"{cid} · filed {today}",
        "\n".join("> " + ln for ln in text.splitlines()) + f"\n\n{whose}")
    doc.reference.update({"status": "ingested", "cid": cid,
                          "ingestedAt": firestore.SERVER_TIMESTAMP})
    print(f"  {doc.id}: FILED — {aid} {cid}")
    return path


def push_answers(conn, fs, dry=False):
    """Answered guidance → the record file and the principal's desk."""
    rows = conn.execute(
        """select g.*, a.name from guidance g join agents a on a.id=g.agent_id
           where g.disposition is not null and g.pushed_at is null"""
    ).fetchall()
    paths = []
    for g in rows:
        print(f"  {g['agent_id']} {g['cid']}: {g['disposition']}")
        if dry:
            continue
        day = (g["answered_at"] or datetime.now(timezone.utc)).date()
        paths.append(append_entry(
            g["agent_id"], g["name"], f"{g['cid']} · answered {day} — {g['disposition']}",
            g["answer"] or ""))
        try:
            from google.cloud import firestore
            fs.collection("guidance").document(g["doc_id"]).update({
                "status": "answered", "disposition": g["disposition"],
                "answer": g["answer"] or "",
                "answeredAt": firestore.SERVER_TIMESTAMP,
            })
        except Exception:
            traceback.print_exc()
            continue  # leave pushed_at null: the next pass retries
        conn.execute("update guidance set pushed_at=now() where id=%s", (g["id"],))
        conn.commit()
    return paths


def main():
    obs.init("guidance")
    check = "--check" in sys.argv
    fs = fs_client()
    docs = filed_docs(fs)
    conn = db.connect()
    db.migrate(conn)
    today = datetime.now(timezone.utc).date()
    print(f"filed notes waiting: {len(docs)}")

    if check:
        for d in docs:
            data = d.to_dict() or {}
            print(f"  {d.id}: uid={data.get('uid')} trader={data.get('trader')} "
                  f"{str(data.get('text'))[:60]!r}")
        push_answers(conn, fs, dry=True)
        print("read-only check complete — no writes.")
        return

    paths, failures = [], 0
    for doc in docs:
        try:
            p = take(conn, doc, today)
            if p:
                paths.append(p)
        except Exception:
            failures += 1
            conn.rollback()
            print(f"  {doc.id}: FAILED (stays filed for the next pass):")
            traceback.print_exc()
    if paths:
        commit(paths, f"guidance: {len(paths)} note(s) filed {today}")

    answered = push_answers(conn, fs)
    if answered:
        commit(answered, f"guidance: {len(answered)} answer(s) {today}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
