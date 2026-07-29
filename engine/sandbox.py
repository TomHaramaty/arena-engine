"""The sandbox: seatings that never enter the record.

An application from an admin principal seats as a *test trader* — `agents.tier`
= 'test'. It runs like any other trader (dispatch, the bell, standing orders,
reflection, the desk) because a test that cannot run tests nothing. What it does
not do is enter the record: it takes no tincture, its prose lives in a gitignored
tree, its journal is never committed, and it is not on the floor.

`jobs.purge` destroys one completely. A real trader is never destroyed — see
design/account-deletion-2026-07-29.md in the trader repo.
"""
import os
import pathlib

TIER = "test"

# The principals allowed to seat sandbox traders. The operator's own uid — which
# also owns ballast, a real trader on the floor, which is exactly why the purge
# names a trader and never a uid. Env wins so CI and a laptop can differ without
# a code change.
DEFAULT_ADMIN_UIDS = ("MW2mQy81o7P7xrzl8T5SblnCxa52",)


def admin_uids():
    raw = os.environ.get("ADMIN_UIDS")
    if raw:
        return {u.strip() for u in raw.split(",") if u.strip()}
    return set(DEFAULT_ADMIN_UIDS)


def is_admin(uid):
    return bool(uid) and uid in admin_uids()


def is_sandbox(agent_row):
    """agent_row: a mapping with a 'tier' key (a DB row or arena.json config)."""
    return (agent_row or {}).get("tier") == TIER


def agent_dir(trader_repo, agent_id, sandbox=None):
    """Where a trader's prose lives.

    A sandbox trader's prose is real prose — the runner reads it to think, the
    reflection rewrites it — but it is not the record: it sits under
    `sandbox/agents/`, which the trader repo gitignores.

    `sandbox=None` resolves by looking: a sandbox tree that exists wins. That
    keeps every caller (context, agent_run, reflect, guidance) free of a DB
    lookup. Seating passes sandbox=True explicitly, because on the first write
    there is nothing to look at yet.
    """
    root = pathlib.Path(trader_repo)
    sb = root / "sandbox" / "agents" / agent_id
    if sandbox is True or (sandbox is None and sb.exists()):
        return sb
    return root / "agents" / agent_id


def in_sandbox(path):
    """True if a path lies inside the gitignored sandbox tree — the guard every
    git-committing caller checks before it stages anything."""
    return "sandbox" in pathlib.Path(path).parts
