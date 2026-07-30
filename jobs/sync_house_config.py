"""Write the house agents' chartered config into the DB, without reseeding.

seed.py is a one-time migration that also rewrites state; when a house charter
is amended, only `agents.config` needs to move. This job carries seed.py's
AGENTS table — the source of truth for house configuration — into the DB and
prints what changed, so an amendment to a constitution and the limits the
engine actually enforces cannot drift apart.

Seated (interview-born) agents are never touched: their config comes from their
principal's countersigned charter, not from this table.

A house agent in the table but missing from the DB is BORN here, provided it
carries a `bench` (batch two and later) and its prose already stands in the
trader repo — the charter is the prose; the DB row is only the limits. Birth is
idempotent and takes the next free tincture from the armory.

Usage: python -m jobs.sync_house_config [--dry]
"""
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

from engine import db, seating
from jobs.seed import AGENTS

TRADER = pathlib.Path(os.environ.get("TRADER_REPO", "/Users/tomharamaty/trader"))
STARTING_CASH = 100000.0


def birth(conn, aid, meta, dry):
    """Create a batch-two house agent: DB rows + tincture. The prose must
    already exist — an agent whose charter is not on the record is not born."""
    if "bench" not in meta:
        print(f"{aid}: not in the DB and carries no bench — skipped (founding "
              "agents are seeded by jobs.seed, never born here)")
        return False
    harness = TRADER / "agents" / aid / "harness.md"
    if not harness.exists():
        print(f"{aid}: REFUSED — no charter at {harness}. Prose first, rows second.")
        return False
    if dry:
        print(f"{aid}: would be born (house, ${STARTING_CASH:,.0f}, "
              f"bench {'+'.join(meta['bench']['symbols'])})")
        return True
    bench = {**meta["bench"], "launch_prices": []}  # stamped at first bell
    conn.execute(
        """insert into agents (id, name, archetype, brain, config, status, tier)
           values (%s,%s,%s,%s,%s,'active','house')
           on conflict (id) do nothing""",
        (aid, meta["name"], meta["archetype"], meta["brain"],
         json.dumps(meta["config"])))
    conn.execute(
        """insert into agent_state (agent_id, cash, peak_equity, launched, bench)
           values (%s,%s,%s,null,%s) on conflict (agent_id) do nothing""",
        (aid, STARTING_CASH, STARTING_CASH, json.dumps(bench)))
    pair = seating.assign_tincture(TRADER / "arena" / "armory.json", aid,
                                   datetime.now(timezone.utc).date())
    tinct = f"tincture № {pair['n']} {pair['name']}" if pair else "slate (armory empty)"
    print(f"{aid}: BORN — house, ${STARTING_CASH:,.0f}, "
          f"bench {'+'.join(meta['bench']['symbols'])}, {tinct}")
    return True


def main():
    dry = "--dry" in sys.argv
    conn = db.connect()
    db.migrate(conn)

    changed = born = 0
    for aid, meta in AGENTS.items():
        row = conn.execute(
            "select config, owner_uid from agents where id=%s", (aid,)
        ).fetchone()
        if not row:
            born += bool(birth(conn, aid, meta, dry))
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
    print(f"\n{changed} agent config(s) {'would be ' if dry else ''}updated, "
          f"{born} {'would be ' if dry else ''}born")


if __name__ == "__main__":
    main()