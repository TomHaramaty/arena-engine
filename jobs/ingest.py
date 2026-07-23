"""Seat-application ingestion: Firestore `applications` → validate → seat.

Each hourly tick: read status='submitted' docs from the open-outcry Firebase
project, validate each packet against the arena floor (engine/seating.py),
then either seat the agent — DB rows, seed prose files in the trader repo,
tincture registration in arena/armory.json, doc → 'seated' — or mark the doc
'rejected' with the Registrar's reasons. Fully automated: the operator's
control is a post-hoc kill switch, never a pre-approval.

Usage:
  python -m jobs.ingest            # ingest (in CI also commits/pushes the trader repo)
  python -m jobs.ingest --check    # read-only: verify Firestore auth, list applications

One application's failure never blocks the others; a failed application stays
'submitted' and is retried next tick (seating is idempotent, so a partial
seating resumes cleanly).
"""
import json
import os
import pathlib
import subprocess
import sys
import traceback
from datetime import datetime, timezone

from engine import db, seating

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRADER = pathlib.Path(os.environ.get("TRADER_REPO", "/Users/tomharamaty/trader"))
STARTING_CASH = 100000.0
BRAIN = "antigravity-gemini"  # the brain every house agent runs (jobs/seed.py)
FIRESTORE_PROJECT = "open-outcry"


def fs_client():
    if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        local = ROOT / ".fs_sa.json"  # gitignored local copy of the SA key
        if local.exists():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(local)
    from google.cloud import firestore
    return firestore.Client(project=FIRESTORE_PROJECT)


def submitted_applications(fs):
    from google.cloud.firestore_v1.base_query import FieldFilter
    q = fs.collection("applications").where(
        filter=FieldFilter("status", "==", "submitted"))
    return list(q.stream())


def taken_ids(conn):
    ids = {r["id"] for r in conn.execute("select id from agents").fetchall()}
    agents_dir = TRADER / "agents"
    if agents_dir.exists():
        ids |= {p.name for p in agents_dir.iterdir() if p.is_dir()}
    return ids


def live_uids(conn):
    return {r["owner_uid"] for r in conn.execute(
        "select owner_uid from agents where status='active' and owner_uid is not null"
    ).fetchall()}


def listed_symbols(conn):
    return {r["symbol"] for r in conn.execute(
        "select symbol from watchlist where status='active'"
    ).fetchall()}


def seat(conn, cleaned, uid, app_id, today):
    """Seat a validated applicant. Idempotent — safe to re-run after a partial
    seating. Returns (trader_repo_paths_touched, tincture_pair_or_None)."""
    aid = cleaned["id"]
    syms = cleaned["benchmark"]["symbols"]
    bench = {"symbols": syms,
             "weights": [round(1.0 / len(syms), 6)] * len(syms),
             "launch_prices": []}  # stamped at first bell (core.bootstrap_launches)
    frac = round(cleaned["max_position_pct"] / 100, 4)
    # crypto_core_cap_pct mirrors the principal's cap: ops.py checks crypto
    # buys only against the crypto sleeve cap, so without it BTC/ETH buys
    # would be uncapped for seated agents.
    config = {"max_single_pct": frac, "crypto_core_cap_pct": frac}
    conn.execute(
        """insert into agents (id, name, archetype, brain, config, status, tier, owner_uid)
           values (%s,%s,%s,%s,%s,'active','seated',%s)
           on conflict (id) do nothing""",
        (aid, cleaned["name"], cleaned["archetype"], BRAIN, json.dumps(config),
         uid))
    conn.execute(
        """insert into agent_state (agent_id, cash, peak_equity, launched, bench)
           values (%s,%s,%s,null,%s) on conflict (agent_id) do nothing""",
        (aid, STARTING_CASH, STARTING_CASH, json.dumps(bench)))
    conn.commit()

    written = seating.write_seed_files(TRADER, cleaned, today, app_id)
    armory_path = TRADER / "arena" / "armory.json"
    pair = seating.assign_tincture(armory_path, aid, today)
    return written + [armory_path], pair


def commit_trader(paths, aid, today):
    """CI only: the tick workflow clones a fresh trader repo and holds its SSH
    key (same push pattern as jobs/agent_run.commit_journal). Local runs leave
    the trader working tree uncommitted for the operator to review."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print(f"  local run — trader repo changes left uncommitted ({len(paths)} paths)")
        return
    repo = str(TRADER)
    subprocess.run(["git", "-C", repo, "add", "--"] + [str(p) for p in paths],
                   check=True)
    staged = subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        return  # nothing new (resume path)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m",
                    f"seat({aid}): {today} chartered from interview"], check=True)
    subprocess.run(["git", "-C", repo, "push", "-q"], check=True)


def process(conn, doc, today):
    from google.cloud import firestore
    data = doc.to_dict() or {}
    uid = str(data.get("uid") or "")
    packet = data.get("packet")
    name = (str(packet.get("name") or "").strip().lower()
            if isinstance(packet, dict) else "")

    # Resume path: a previous run seated this uid+name but the doc update
    # failed. The agent row is the source of truth — finish the paperwork.
    if name and uid:
        r = conn.execute("select 1 from agents where id=%s and owner_uid=%s",
                         (name, uid)).fetchone()
        if r:
            cleaned, reasons = seating.validate_packet(
                packet, taken_ids=set(), has_live_agent=False,
                listed_symbols=None, today=today)
            if cleaned and not reasons:
                paths, _ = seat(conn, cleaned, uid, doc.id, today)
                commit_trader(paths, name, today)
            doc.reference.update({"status": "seated", "agent_id": name,
                                  "seatedAt": firestore.SERVER_TIMESTAMP})
            print(f"  {doc.id}: resumed — {name} already seated")
            return

    cleaned, reasons = seating.validate_packet(
        packet,
        taken_ids=taken_ids(conn),
        has_live_agent=bool(uid) and uid in live_uids(conn),
        listed_symbols=listed_symbols(conn),
        today=today)
    if not uid:
        reasons.append("The application carries no principal. Sign in and "
                       "interview again.")
    if reasons:
        doc.reference.update({"status": "rejected", "reasons": reasons,
                              "rejectedAt": firestore.SERVER_TIMESTAMP})
        print(f"  {doc.id}: REJECTED — " + " | ".join(reasons))
        return

    paths, pair = seat(conn, cleaned, uid, doc.id, today)
    commit_trader(paths, cleaned["id"], today)
    doc.reference.update({"status": "seated", "agent_id": cleaned["id"],
                          "seatedAt": firestore.SERVER_TIMESTAMP})
    tinct = (f"tincture № {pair['n']} {pair['name']}" if pair
             else "tincture pending minting (slate)")
    print(f"  {doc.id}: SEATED — {cleaned['id']} · {tinct}")


def main():
    check = "--check" in sys.argv
    fs = fs_client()
    docs = submitted_applications(fs)
    print(f"submitted applications: {len(docs)}")
    if check:
        for d in docs:
            data = d.to_dict() or {}
            pkt = data.get("packet")
            name = pkt.get("name") if isinstance(pkt, dict) else "?"
            print(f"  {d.id}: uid={data.get('uid')} name={name}")
        print("read-only check complete — no writes.")
        return
    if not docs:
        return

    conn = db.connect()
    db.migrate(conn)
    today = datetime.now(timezone.utc).date()
    failures = 0
    for doc in docs:
        try:
            process(conn, doc, today)
        except Exception:
            failures += 1
            conn.rollback()
            print(f"  {doc.id}: FAILED (left submitted for retry next tick):")
            traceback.print_exc()
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
