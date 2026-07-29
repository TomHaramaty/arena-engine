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

# The principals allowed to seat sandbox traders.
#
# By EMAIL first, and that is not a detail: the test loop deletes the Firebase
# Auth user and signs in again, which mints a new uid every cycle. A uid
# allowlist would work exactly once. The uids are kept as a second door for an
# account whose application somehow carries no address.
#
# Both accounts here already own real traders on the floor — gmail owns ballast,
# withinapp owns fury — which is exactly why the purge names a trader and never
# a principal.
DEFAULT_ADMIN_EMAILS = ("tomharamaty@gmail.com", "tom@withinapp.ai")
DEFAULT_ADMIN_UIDS = ("MW2mQy81o7P7xrzl8T5SblnCxa52",
                      "bKW6PZCBvuh2DBP3qsyZVaBnsxA3")


def _from_env(name, default):
    raw = os.environ.get(name)
    values = raw.split(",") if raw else default
    return {v.strip() for v in values if v.strip()}


def admin_uids():
    """Case-sensitive: a Firebase uid is a mixed-case opaque string."""
    return _from_env("ADMIN_UIDS", DEFAULT_ADMIN_UIDS)


def admin_emails():
    return {e.lower() for e in _from_env("ADMIN_EMAILS", DEFAULT_ADMIN_EMAILS)}


def is_admin(uid, email=None):
    if email and str(email).strip().lower() in admin_emails():
        return True
    return bool(uid) and str(uid) in admin_uids()


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
