"""The principal's name on the floor — Firestore `users/{uid}` → agents.config.

A principal may ask to be named beside the trader they chartered, or stay
anonymous. That is a display preference, not a clause of the charter: it is
theirs to change at any hour, so it lives in Firestore where their own pages
write it, and it is carried into `agents.config` here so jobs/site.py can
publish it with everything else the floor shows.

Anonymous is the default and the fallback. A name reaches the config only when
the principal has asked to be named; a principal who never chose is never named,
and one who turns it off is gone from the next build of the floor. A failed read
leaves the config exactly as it was — a display preference must never fail a
tick that is also publishing the record.

Usage: python -m jobs.credit [--dry]   (needs DATABASE_URL + Firestore creds)
"""
import json
import re
import sys

from engine import db
from engine import observability as obs
from jobs.ingest import fs_client

MAX_NAME = 60
ANON = {"name": "", "show": False}
# The floor is not a billboard: a name that carries a link is not a name.
LINKY = re.compile(r"https?://|www\.|\S+\.(?:com|net|org|io|xyz|co|ai)\b", re.I)


def clean_name(raw):
    """A person's name as it will be printed on a public page: one line, no
    control characters, no link bait, 60 characters at most. An empty result
    means there is nothing that may be shown."""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(raw or ""))
    s = s.replace("<", " ").replace(">", " ")  # a name is never markup
    s = re.sub(r"\s+", " ", s).strip()[:MAX_NAME]
    return "" if LINKY.search(s) else s


def credit_of(snap):
    """{name, show} from a users doc. Either half missing means anonymous, and
    a name that does not survive cleaning cannot be shown."""
    data = (snap.to_dict() or {}) if snap is not None and snap.exists else {}
    c = data.get("credit")
    if not isinstance(c, dict):
        return dict(ANON)
    name = clean_name(c.get("name"))
    if not name or not c.get("show"):
        return dict(ANON)
    return {"name": name, "show": True}


def wanted(fs, uid):
    return credit_of(fs.collection("users").document(uid).get())


def sync(conn, fs, dry=False):
    """Carry every seated agent's owner preference into its config. Returns
    (changed, failed)."""
    rows = conn.execute(
        "select id, config, owner_uid from agents "
        "where status='active' and owner_uid is not null order by id"
    ).fetchall()
    print(f"seated agents: {len(rows)}")
    seen, changed, failed = {}, 0, 0
    for row in rows:
        uid = row["owner_uid"]
        if uid not in seen:
            try:
                seen[uid] = wanted(fs, uid)
            except Exception as e:  # one unreadable profile is not a tick failure
                failed += 1
                print(f"  {row['id']}: could not read the principal — left as is ({e})")
                seen[uid] = None
        want = seen[uid]
        if want is None:
            continue
        config = row["config"] if isinstance(row["config"], dict) else {}
        have = config.get("credit") if isinstance(config.get("credit"), dict) else dict(ANON)
        if have == want:
            continue
        changed += 1
        print(f"  {row['id']}: {'named ' + repr(want['name']) if want['show'] else 'anonymous'}")
        if not dry:
            conn.execute("update agents set config=%s where id=%s",
                         (json.dumps({**config, "credit": want}), row["id"]))
    if not dry:
        conn.commit()
    return changed, failed


def main():
    obs.init("credit")
    dry = "--dry" in sys.argv
    conn = db.connect()
    db.migrate(conn)
    changed, failed = sync(conn, fs_client(), dry=dry)
    print(f"{changed} credit(s) {'would be ' if dry else ''}updated"
          + (f" · {failed} profile(s) unreadable" if failed else ""))


if __name__ == "__main__":
    main()
