"""What a refused operation actually was, and what a person may be told about it.

Every rejection the engine writes is a sentence addressed to a brain: precise,
technical, and often naming the operation type the brain must file next. Those
sentences then travel, unchanged, into two places written for people — the
floor's Trades tab and the letter a principal reads over breakfast — where
`jobs/letter.py` introduced each one with the words "was REFUSED by your
constitution".

Measured on 2026-08-05 over the whole record: of 73 refusals, six were a
constitution. Fifteen were `error: deadlock detected`, seventeen were a missing
environment variable, one was a data-source outage, and thirty-four were an
order for a symbol whose grant had just failed for one of those reasons. So
about nine in ten times a principal was told their trader's own rulebook had
stopped it, the arena had broken and blamed the trader.

Two rules come out of that, and this module exists to keep them:

  1. A cause is only claimed when it is known. There is a fourth verdict here,
     `UNCLASSIFIED`, and it says nothing about why — which is the honest thing
     to say about a sentence this module has not seen before, and is what a
     future engine fault will land in rather than being read out as a
     constitution.
  2. Nothing internal is published. Operation type names, environment
     variables, SQL and exception text are addressed to a brain, not to a
     reader, and `public_note` never lets them out.
"""
import re

CONSTITUTION = "constitution"   # the charter, enforced in code, said no
ENGINE = "engine"               # the arena failed; this is not about the trader
OPERATION = "operation"         # the order itself was incomplete or ineligible
UNCLASSIFIED = "unclassified"   # a sentence this module does not recognise

# Ordered, and the order matters: the truthful "not on watchlist" message now
# quotes the reason the grant failed, so an engine fault inside it must be seen
# before the surrounding operation wording is.
_RULES = (
    (ENGINE, re.compile(
        r"^error:"                        # any exception that reached the handler
        r"|could not reach the data source"
        r"|data source unreachable"
        r"|no engine price for", re.I)),
    (CONSTITUTION, re.compile(
        r"cap .*breached"
        r"|no meaningful capacity"
        r"|long-only"
        r"|unchartered", re.I)),
    (OPERATION, re.compile(
        r"not on watchlist"
        r"|does not resolve to a quotable instrument"
        r"|needs thesis"
        r"|needs positive"
        r"|must be buy or sell"
        r"|kind must be"
        r"|invalid symbol"
        r"|no position to protect"
        r"|is a sell-side protection", re.I)),
)

# Identifiers that are part of the machine's vocabulary and none of a reader's.
_INTERNAL = re.compile(
    r"\b(watchlist_request|place_order|register_standing_order|cancel_order"
    r"|hypothesis_op|guidance_response|journal_entry)\b")

_ENGINE_NOTE = ("the arena could not complete this — a fault on our side, "
                "not a decision about your trader")
_UNKNOWN_NOTE = "the order was not accepted"


def classify(reason):
    """One of CONSTITUTION / ENGINE / OPERATION / UNCLASSIFIED."""
    text = (reason or "").strip()
    if not text:
        return UNCLASSIFIED
    for cause, pattern in _RULES:
        if pattern.search(text):
            return cause
    return UNCLASSIFIED


def public_note(reason):
    """The refusal as a person may read it: no internal identifiers, no SQL,
    no exception text, and no cause claimed that is not known."""
    cause = classify(reason)
    if cause == ENGINE:
        return _ENGINE_NOTE
    if cause == UNCLASSIFIED:
        return _UNKNOWN_NOTE
    # A constitutional refusal is already the clearest sentence anyone could
    # write about it ("single-position cap 20% of equity breached"), and an
    # ineligible order usually is too — they only need their machine nouns
    # turned back into English, and their first line taken.
    text = (reason or "").split("\n")[0].strip()
    text = _INTERNAL.sub(lambda m: m.group(0).replace("_", " "), text)
    return text


def label(cause):
    """The word stamped beside the row on the floor and in a letter.

    "BLOCKED" says a rule stopped the trader. That is true of six refusals in
    the record and false of the other sixty-seven, where the order simply never
    reached the market.
    """
    return "BLOCKED" if cause == CONSTITUTION else "NOT SENT"


def attribution(cause):
    """How the fact pack a letter is written from should introduce the refusal.

    The model is never handed the word "constitution" for a refusal that was
    not one — it writes in the register it is given, and it wrote "The
    constitution blocked both transactions" because that is what it was told.
    """
    return {
        CONSTITUTION: "was refused by your constitution",
        ENGINE: "did not reach the market because the arena failed, "
                "which is our fault and not your trader's",
        OPERATION: "was not accepted, because the order itself was incomplete "
                   "or the instrument was not one this book may trade",
    }.get(cause, "was not accepted")
