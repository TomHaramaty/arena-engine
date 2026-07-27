"""Write the house agents' chartered config into the DB, without reseeding.

seed.py is a one-time migration that also rewrites state; when a house charter
is amended, only `agents.config` needs to move. This job carries seed.py's
AGENTS table — the source of truth for house configuration — into the DB and
prints what changed, so an amendment to a constitution and the limits the
engine actually enforces cannot drift apart.

Seated (interview-born) agents are never touched: their config comes from their
principal's countersigned charter, not from this table.

Usage: python -m jobs.sync_house_config [--dry]
"""
import json
import sys

from engine import db
from jobs.seed import AGENTS


def main():
    dry = "--dry" in sys.argv
    conn = db.connect()
    db.migrate(conn)

    changed = 0
    for aid, meta in AGENTS.items():
        row = conn.execute(
            "select config, owner_uid from agents where id=%s", (aid,)
        ).fetchone()
        if not row:
            print(f"{aid}: not in the DB — skipped")
            continue
        if row["owner_uid"]:
            print(f"{aid}: seated agent (has a principal) — refusing to touch")
            continue
        current, wanted = row["config"] or {}, meta["config"]
        # Preserve keys the seed table does not speak for (avatar, and anything
        # a later feature adds); this job owns limits, not identity.
        merged = {**current, **wanted}
        if merged == current:
            continue
        print(f"{aid}: {json.dumps(current)}\n    -> {json.dumps(merged)}")
        changed += 1
        if not dry:
            conn.execute(
                "update agents set config=%s where id=%s", (json.dumps(merged), aid)
            )
    if not dry:
        conn.commit()
    print(f"\n{changed} agent config(s) {'would be ' if dry else ''}updated")


if __name__ == "__main__":
    main()