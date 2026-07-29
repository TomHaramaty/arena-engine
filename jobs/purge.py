"""Delete a trader and its principal's private data — admin, CLI, dry by default.

    python -m jobs.purge --trader <id>                 # dry run: prints, touches nothing
    python -m jobs.purge --trader <id> --yes           # execute
    python -m jobs.purge --trader <id> --yes --auth    # also delete the Firebase Auth user
    python -m jobs.purge --trader <id> --retire --yes  # the record-safe path for a real trader

Four laws, enforced below, not by care:

1. **Dry by default.** No --yes, no writes. It prints the exact rows, documents
   and paths it would touch, and exits.
2. **A trader is named, never a uid.** The owner is derived from the trader row,
   so the blast radius is always something that was typed. The operator's own uid
   owns ballast — a real trader on the floor — which is why a uid-wide purge does
   not exist here.
3. **A real trader cannot be purged.** Anything that is not a sandbox seating
   refuses --yes and points at --retire. There is no override flag: erasing a
   real trader would have to be a code change, deliberately.
4. **Shared data is never touched.** `watchlist` (its symbols are held in common,
   and its rows are the provenance of other traders' positions), `ticks` (market
   data belongs to nobody), and `arena/armory.json` (the registry says a pair is
   never reassigned; the sandbox sidesteps that by never taking one).

--retire is the deletion a real principal gets: the trader leaves the floor and
stops running, its prose and journals stand untouched with a dated withdrawal
entry appended, and the private surfaces — the desk thread, the draft, the
Firestore guidance mirrors — are cleared. The record is not edited; it is closed.

See design/account-deletion-2026-07-29.md in the trader repo.
"""
import argparse
import os
import pathlib
import shutil
import sys
from datetime import datetime, timezone

from engine import db, sandbox

TRADER = pathlib.Path(os.environ.get("TRADER_REPO", "/Users/tomharamaty/trader"))

# Deletion order: children before parents. `operations` hangs off `runs`, so it
# goes first and by run id; everything else keys on agent_id directly.
AGENT_TABLES = (
    "guidance",
    "triggers_fired",
    "fills",
    "orders",
    "positions",
    "equity_marks",
    "runs",
    "agent_state",
)


def load(conn, trader_id):
    row = conn.execute(
        "select id, name, tier, status, owner_uid from agents where id=%s",
        (trader_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"no such trader: {trader_id}")
    return row


def db_counts(conn, trader_id):
    """What the purge would delete, table by table — the dry run's whole point."""
    counts = {}
    r = conn.execute(
        """select count(*) c from operations o join runs r on r.id=o.run_id
           where r.agent_id=%s""", (trader_id,)).fetchone()
    counts["operations"] = r["c"]
    for t in AGENT_TABLES:
        r = conn.execute(f"select count(*) c from {t} where agent_id=%s",
                         (trader_id,)).fetchone()
        counts[t] = r["c"]
    counts["agents"] = 1
    return counts


def purge_db(conn, trader_id):
    conn.execute(
        """delete from operations where run_id in
           (select id from runs where agent_id=%s)""", (trader_id,))
    for t in AGENT_TABLES:
        conn.execute(f"delete from {t} where agent_id=%s", (trader_id,))
    conn.execute("delete from agents where id=%s", (trader_id,))
    conn.commit()


# ---------- Firestore ----------

def fs_docs(fs, uid, trader_id, sole_trader=True):
    """The documents this deletion may take, as (label, DocumentReference).

    Scoped to ONE trader, not to the principal. Both admin accounts own a real
    trader on the floor as well as whatever test trader is being purged, so a
    sweep by uid would delete the profile that carries the real trader's letter
    address and the application that is its charter's provenance.

    sole_trader=False (the principal still owns other traders) therefore keeps
    `users/{uid}` and every application belonging to another trader.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter
    out = []
    if sole_trader:
        # The profile is the principal's, not the trader's — it only goes when
        # the last trader does.
        ref = fs.collection("users").document(uid)
        if ref.get().exists:
            out.append((f"users/{uid}", ref))
    # The draft is whatever interview is in flight — for a test loop that is the
    # leftover of the seating being purged.
    ref = fs.collection("drafts").document(uid)
    if ref.get().exists:
        out.append((f"drafts/{uid}", ref))
    desk = fs.collection("desks").document(f"{uid}_{trader_id}")
    if desk.get().exists:
        out.append((f"desks/{uid}_{trader_id}", desk))
    for d in fs.collection("guidance").where(
            filter=FieldFilter("uid", "==", uid)).stream():
        if (d.to_dict() or {}).get("trader") == trader_id:
            out.append((f"guidance/{d.id}", d.reference))
    for d in fs.collection("applications").where(
            filter=FieldFilter("uid", "==", uid)).stream():
        seated_as = (d.to_dict() or {}).get("agent_id")
        # An application with no agent_id was never seated — a rejection or an
        # in-flight submission, the provenance of nothing.
        if seated_as == trader_id or not seated_as:
            out.append((f"applications/{d.id}", d.reference))
    return out


def other_traders(conn, uid, trader_id):
    """The principal's other traders. Their existence makes a purge narrower."""
    if not uid:
        return []
    return [r["id"] for r in conn.execute(
        "select id from agents where owner_uid=%s and id<>%s order by id",
        (uid, trader_id)).fetchall()]


def keep_for_retire(label):
    """On --retire the record stands and the rooms close. The profile stays —
    the principal still signs in — and so do their applications, which are the
    provenance of a charter that is still on the floor."""
    return label.startswith("users/") or label.startswith("applications/")


# ---------- the trader repo ----------

def prose_dir(trader_id):
    return sandbox.agent_dir(TRADER, trader_id)


WITHDRAWAL = """# {date} — withdrawn

The principal withdrew this trader on {date}. It has stopped running and has
left the floor. Nothing above this line has been altered: every entry, every
principle and every fill stands as it was written.
"""


def write_withdrawal(trader_id, today):
    """A retirement is an appended entry, never an edit — CLAUDE.md rule 1."""
    d = prose_dir(trader_id)
    path = d / "journal" / f"{today}-withdrawn.md"
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WITHDRAWAL.format(date=today), encoding="utf-8")
    return path


# ---------- the report ----------

def plan(conn, fs, row, retire):
    """Everything that would happen, gathered before anything happens."""
    trader_id, uid = row["id"], row["owner_uid"]
    others = other_traders(conn, uid, trader_id)
    p = {
        "trader": trader_id,
        "tier": row["tier"],
        "uid": uid,
        "retire": retire,
        "others": others,
        "db": {} if retire else db_counts(conn, trader_id),
        "docs": [],
        "paths": [],
    }
    if uid and fs is not None:
        docs = fs_docs(fs, uid, trader_id, sole_trader=not others)
        p["docs"] = [lbl for lbl, _ in docs
                     if not (retire and keep_for_retire(lbl))]
    d = prose_dir(trader_id)
    if retire:
        p["paths"] = [str(d / "journal" / "<today>-withdrawn.md") + "  (appended)"]
    elif d.exists():
        p["paths"] = [str(d) + "/  (whole tree)"]
    return p


def report(p):
    head = "RETIRE" if p["retire"] else "PURGE"
    print(f"\n{head}  {p['trader']}   tier={p['tier']}  owner={p['uid'] or '-'}")
    print("-" * 60)
    if p["others"]:
        print(f"  the principal also holds {', '.join(p['others'])} — their data "
              "is out of scope")
    if p["retire"]:
        print("  postgres  agents.status → 'withdrawn' (no rows deleted)")
    else:
        total = sum(p["db"].values())
        print(f"  postgres  {total} row(s)")
        for t, c in p["db"].items():
            if c:
                print(f"              {t:<16}{c}")
    print(f"  firestore {len(p['docs'])} document(s)")
    for lbl in p["docs"]:
        print(f"              {lbl}")
    print(f"  repo      {len(p['paths'])} path(s)")
    for path in p["paths"]:
        print(f"              {path}")
    print("-" * 60)
    print("  never touched: watchlist · ticks · armory.json"
          + ("  · the record's prose" if p["retire"] else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobs.purge")
    ap.add_argument("--trader", required=True, help="the trader id to remove")
    ap.add_argument("--yes", action="store_true",
                    help="execute (without it this is a dry run)")
    ap.add_argument("--retire", action="store_true",
                    help="record-safe: withdraw a real trader instead of erasing it")
    ap.add_argument("--auth", action="store_true",
                    help="also delete the Firebase Auth user (full purge only)")
    args = ap.parse_args(argv)

    conn = db.connect()
    row = load(conn, args.trader)

    if not args.retire and row["tier"] != sandbox.TIER:
        raise SystemExit(
            f"\n{row['id']} is a real trader (tier '{row['tier']}'), and the record "
            "does not lose entries.\nUse --retire to withdraw it: it leaves the "
            "floor and stops running, its journals stand.\n")
    if args.retire and row["tier"] == sandbox.TIER:
        print(f"note: {row['id']} is a sandbox trader — retiring one is allowed, "
              "but --yes alone erases it outright.")

    try:
        from jobs.ingest import fs_client
        fs = fs_client()
    except Exception as e:  # credentials missing on a laptop is not a failure
        print(f"firestore unavailable ({type(e).__name__}) — reporting DB + repo only")
        fs = None

    p = plan(conn, fs, row, args.retire)
    report(p)

    if not args.yes:
        print("\ndry run — nothing was touched. Add --yes to execute.\n")
        return 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.retire:
        conn.execute("update agents set status='withdrawn' where id=%s",
                     (row["id"],))
        conn.commit()
        path = write_withdrawal(row["id"], today)
        print(f"  postgres  {row['id']} → withdrawn")
        print(f"  repo      {path or 'withdrawal entry already written'}")
    else:
        # Check the prose tree BEFORE touching a row: a refusal after the
        # deletes would leave a trader half-gone, which is worse than either
        # outcome. The tier gate above is the real guard; this is its braces.
        d = prose_dir(row["id"])
        if d.exists() and not sandbox.in_sandbox(d):
            raise SystemExit(f"refusing to delete outside the sandbox: {d}")
        purge_db(conn, row["id"])
        print(f"  postgres  {sum(p['db'].values())} row(s) deleted")
        if d.exists():
            shutil.rmtree(d)
            print(f"  repo      {d} removed")

    if fs is not None and row["owner_uid"]:
        n = 0
        for lbl, ref in fs_docs(fs, row["owner_uid"], row["id"],
                                sole_trader=not p["others"]):
            if args.retire and keep_for_retire(lbl):
                continue
            ref.delete()
            n += 1
        print(f"  firestore {n} document(s) deleted")

    if args.auth and p["others"]:
        print(f"  auth      NOT deleted — the principal still holds "
              f"{', '.join(p['others'])}. Deleting the sign-in would orphan "
              "a live trader.")
    elif args.auth and not args.retire and row["owner_uid"]:
        try:
            import firebase_admin
            from firebase_admin import auth as fb_auth
            if not firebase_admin._apps:
                firebase_admin.initialize_app()
            fb_auth.delete_user(row["owner_uid"])
            print(f"  auth      user {row['owner_uid']} deleted")
        except Exception as e:
            print(f"  auth      NOT deleted ({type(e).__name__}: {e}) — "
                  "delete it by hand in the Firebase console")

    print(f"\n{'retired' if args.retire else 'purged'}: {row['id']}\n")
    if not args.retire:
        print("the trader repo's sandbox tree is gitignored — nothing to commit.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
