"""The letter — what a trader sends its principal.

Design: trader repo design/letters-2026-07-29.md.

Two sources, deliberately split:

  facts        arena.json, the same projection the floor renders. A letter can
               therefore never contradict the floor, because it is built from
               the floor's own numbers.
  preferences  Postgres (agents.config) and the owner's address. These are
               private and must never appear in the published file.

The law from §1 of the design: **the model writes connective prose only and
never a quantity.** This module injects every number itself. `no_quantities`
is the enforcement, and it is not advisory — a letter whose prose states a
figure is not sent.

This module is pure: it decides and it builds. It performs no I/O and sends
nothing, so the whole of the interesting behaviour is testable without a
network, a database, or a mailbox.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- eligibility

#: The closed list from ruling 1. An event is something that happened to the
#: RECORD. The portfolio being worth a different number than yesterday is
#: explicitly not an event — if it were, every day would qualify and the ruling
#: would mean nothing.
EVENT_KINDS = ("fill", "armed", "pulled", "blocked")

#: Record identifiers — P2, H1, C3. These are pointers INTO the record rather
#: than claims about it, and a trader must be able to name the rule it acted
#: on, so they survive the quantity guard. Nothing else with a digit does.
RECORD_ID = re.compile(r"\b[PHC]\d{1,2}\b")

_TAG = re.compile(r"<[^>]+>")
_DIGIT = re.compile(r"\d")


class ProseStatesQuantity(ValueError):
    """Model-written prose tried to state a number. The letter is not sent."""


def no_quantities(prose: dict) -> None:
    """Raise unless every value is free of quantities.

    Record identifiers are allowed; a bare digit anywhere else is refused. The
    check runs on the text with markup stripped, so a number hidden inside an
    attribute cannot slip through.
    """
    for key, value in sorted(prose.items()):
        bare = RECORD_ID.sub("", _TAG.sub("", str(value)))
        found = _DIGIT.findall(bare)
        if found:
            raise ProseStatesQuantity(
                f"prose[{key!r}] states a quantity ({''.join(found)!r}); "
                "every number must be injected from the record"
            )


def day_events(tape: list, agent_id: str, day: str) -> list:
    """Events on the record for this trader on this day, newest first.

    `day` is the tape's own label prefix, e.g. "Jul 28" — the tape is what the
    floor shows, and matching its labels keeps the letter and the floor in
    agreement about what happened when.
    """
    return [
        e for e in tape
        if e.get("agent") == agent_id
        and e.get("event") in EVENT_KINDS
        and str(e.get("when", "")).startswith(day)
    ]


def eligible(cadence: str, events: list, *, is_reflection_day: bool = False,
             answered_guidance: int = 0, rulebook_changed: bool = False) -> tuple:
    """Should a letter go out? Returns (send: bool, reason: str).

    The reason is returned rather than logged so callers can record WHY a
    trader stayed silent — a quiet trader is a fact about the strategy, not an
    error, and it should be legible afterwards.
    """
    cadence = (cadence or "daily").lower()

    if cadence == "off":
        return False, "principal turned letters off"

    happened = bool(events) or answered_guidance > 0 or rulebook_changed

    if cadence == "weekly":
        if not is_reflection_day:
            return False, "weekly: not the reflection day"
        if not (happened or is_reflection_day):
            return False, "weekly: nothing happened this week"
        return True, "weekly: the reflection ran"

    # daily — ruling 1: only on a day something actually happened
    if is_reflection_day:
        return True, "the reflection ran"
    if not happened:
        return False, "nothing happened on the record today"
    if answered_guidance:
        return True, "the trader answered its principal"
    if rulebook_changed:
        return True, "the rulebook changed"
    return True, f"{len(events)} event(s) on the record"


# ------------------------------------------------------------------- the facts

def round_trip(tape: list, agent_id: str, sell: dict) -> dict | None:
    """Match a sell to the buy that opened it, and do the arithmetic here.

    Never take a return from the trader's own prose. Observed 2026-07-28:
    Catalyst's journal said "+1.3%" on a Visa exit whose fills were 361.892025
    to 366.040115 — which is +1.15%. The prose was written before the fill.
    The fills are the record; the prose is not.
    """
    buys = [
        e for e in tape
        if e.get("agent") == agent_id and e.get("event") == "fill"
        and e.get("side") == "buy" and e.get("symbol") == sell.get("symbol")
        and e.get("t", 0) < sell.get("t", 0)
    ]
    if not buys:
        return None
    buy = max(buys, key=lambda e: e.get("t", 0))
    entry, exit_ = float(buy["price"]), float(sell["price"])
    qty = float(sell.get("qty") or 0)
    return {
        "buy": buy,
        "entry": entry,
        "ret": exit_ / entry - 1 if entry else 0.0,
        "gain": (exit_ - entry) * qty,
        "proceeds": exit_ * qty,
    }


def payload(arena: dict, agent_id: str, day: str) -> dict:
    """Everything a letter renders, lifted from the published projection."""
    agents = arena.get("agents") or []
    agent = next((a for a in agents if a.get("id") == agent_id), None)
    if agent is None:
        raise KeyError(f"no such trader on the floor: {agent_id}")

    tape = arena.get("tape") or []
    events = day_events(tape, agent_id, day)

    fills = []
    for e in (x for x in events if x.get("event") == "fill"):
        fills.append({**e, "round_trip": round_trip(tape, agent_id, e) if e.get("side") == "sell" else None})

    board = sorted(agents, key=lambda a: -(a.get("ret") or 0.0))
    floor_avg = (sum((a.get("ret") or 0.0) for a in agents) / len(agents)) if agents else 0.0

    testing = [h for h in (agent.get("hypotheses") or []) if h.get("status") == "testing"]

    return {
        "agent": {
            "id": agent.get("id"),
            "name": agent.get("name"),
            "archetype": agent.get("archetype"),
            "chartered_by": agent.get("chartered_by") or "the house",
        },
        "day": day,
        "fills": fills,
        "pulled": [e for e in events if e.get("event") == "pulled"],
        "armed": [e for e in events if e.get("event") == "armed"],
        "blocked": [e for e in events if e.get("event") == "blocked"],
        "stand": {
            "equity": agent.get("equity"),
            "ret": agent.get("ret"),
            "alpha": agent.get("alpha"),
            "cash_pct": agent.get("cash_pct"),
            "benchmark": agent.get("benchmark_label"),
            "max_dd": agent.get("max_dd"),
        },
        "positions": sorted(agent.get("positions") or [], key=lambda p: -(p.get("value") or 0)),
        "curve": agent.get("curve") or [],
        "hypothesis": testing[0] if testing else None,
        "board": [{"id": a.get("id"), "name": a.get("name"), "ret": a.get("ret") or 0.0} for a in board],
        "floor_avg": floor_avg,
        # the face is a hosted raster: email strips inline SVG and blocks data:
        "avatar_url": f"https://conviction-league.com/avatars/{agent.get('id')}.png",
    }
