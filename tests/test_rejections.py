"""What a refused operation is allowed to say to a person.

Every reason string below is verbatim from the production `operations` table on
2026-08-05. The counts in the module docstring are real, and so is the letter
the operator forwarded: "The constitution blocked both transactions because
neither ticker is on the watchlist" went to a principal over a missing
environment variable.
"""
from engine import rejections as rj

# The whole record's refusals, by shape, with how often each occurred.
DEADLOCK = ("error: deadlock detected\n"
            "LINE 1: select 1 from watchlist where symbol=$1 and status='active'")
MISSING_KEY = "error: 'FINNHUB_KEY'"
UNREACHABLE = ("could not reach the data source to resolve LMT (search for LMT "
               "failed: data source unreachable) — this is not a verdict on the "
               "symbol; request it again")
NOT_LISTED = "NOW not on watchlist — file watchlist_request first"
CAP = "single-position cap 20% of equity breached"
LONG_ONLY = "no position to protect (long-only)"
CAPACITY = "no meaningful capacity: $0 left under single-position cap 20%"


def test_the_six_real_constitutional_refusals_are_named_as_such():
    assert rj.classify(CAP) == rj.CONSTITUTION
    assert rj.classify(LONG_ONLY) == rj.CONSTITUTION
    assert rj.classify(CAPACITY) == rj.CONSTITUTION


def test_every_engine_fault_in_the_record_is_named_as_ours():
    for reason in (DEADLOCK, MISSING_KEY, UNREACHABLE):
        assert rj.classify(reason) == rj.ENGINE, reason


def test_a_missing_prerequisite_is_neither_the_charter_nor_a_fault():
    assert rj.classify(NOT_LISTED) == rj.OPERATION


def test_an_unrecognised_reason_claims_no_cause_at_all():
    """The rule that keeps this module honest as the engine grows: a sentence
    written after this file was last read must not be narrated as a
    constitution just because it is the default."""
    assert rj.classify("some refusal invented next year") == rj.UNCLASSIFIED
    assert rj.attribution(rj.UNCLASSIFIED) == "was not accepted"
    assert "constitution" not in rj.attribution(rj.UNCLASSIFIED)


def test_no_engine_fault_ever_reaches_a_reader():
    """SQL, environment variables and exception text are addressed to a brain."""
    for reason in (DEADLOCK, MISSING_KEY, UNREACHABLE):
        note = rj.public_note(reason)
        assert "FINNHUB" not in note
        assert "select" not in note.lower() and "LINE 1" not in note
        assert "error:" not in note
        assert "fault on our side" in note


def test_an_operation_name_is_turned_back_into_english():
    """`watchlist_request` is a machine noun. The floor and the letters are not
    written for machines."""
    note = rj.public_note(NOT_LISTED)
    assert "watchlist_request" not in note
    assert "watchlist request" in note


def test_the_truthful_refusal_carries_the_fault_that_caused_it():
    """runner/ops.py now quotes the failed grant inside the order's refusal, so
    an engine fault wrapped in an order's wording must still read as ours."""
    wrapped = ("LMT is not tradable: your watchlist_request for it in this same "
               "block was refused — " + UNREACHABLE)
    assert rj.classify(wrapped) == rj.ENGINE
    assert "fault on our side" in rj.public_note(wrapped)


def test_a_constitution_keeps_its_own_words():
    """When the charter really did stop the trade, the charter's sentence is
    the clearest one anyone could write, and it survives intact."""
    assert rj.public_note(CAP) == CAP
    assert rj.attribution(rj.CONSTITUTION) == "was refused by your constitution"


def test_only_a_constitution_is_stamped_blocked():
    assert rj.label(rj.CONSTITUTION) == "BLOCKED"
    assert rj.label(rj.ENGINE) == "NOT SENT"
    assert rj.label(rj.UNCLASSIFIED) == "NOT SENT"


def test_an_empty_reason_says_nothing_it_does_not_know():
    assert rj.classify("") == rj.UNCLASSIFIED
    assert rj.public_note(None) == "the order was not accepted"
