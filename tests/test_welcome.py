"""The welcome: the copy's laws, the once-ever guard, and the outcomes."""

import pytest

from jobs.letter import LETTER_INSERT
from jobs.welcome import (
    SITE,
    SENDER,
    already_welcomed,
    compose,
    envelope,
    render_html,
    send_welcome,
)


# -------------------------------------------------------------------- the copy

def test_the_subject_is_the_welcome_itself():
    subject, _ = compose("Ballast")
    assert subject == "Welcome to Conviction League"


def test_the_body_speaks_to_the_desk_and_owns_the_simulation():
    _, text = compose("Ballast")
    assert "conviction-league.com/desk" in text
    assert "simulated" in text


def test_the_desk_is_a_conversation_not_a_mailbox():
    """Operator ruling, v4: chat and coach, not leave-a-note-and-wait."""
    _, text = compose("Ballast")
    assert "chat with Ballast" in text
    assert "coach it" in text


def test_the_silence_expectation_is_the_default_fourth_beat():
    """The paragraph the whole email exists for: quiet is design, not
    breakage. Ruling 1 of the letters means the next email may be weeks out."""
    _, text = compose("Ballast", cadence="daily")
    assert "when something happens" in text
    assert "waiting for its moment" in text


def test_a_principal_who_declined_letters_is_not_promised_letters():
    _, text = compose("Ballast", cadence="off")
    assert "waiting for its moment" not in text
    assert "only email you will get" in text
    assert "turn letters on" in text


def test_it_opens_with_a_welcome_and_means_it():
    _, text = compose("Ballast")
    assert text.startswith("Welcome to Conviction League.")
    assert "excited to have you" in text


def test_the_open_channel_is_stated_not_implied():
    """Operator rulings, v3-v6: the reply ask must say plainly that questions
    and feedback of any kind are appreciated and answered — in plain words,
    not product language ('open channel' read as weird phrasing), and in the
    house's voice, not the founder's."""
    _, text = compose("Ballast")
    assert "questions, ideas, feedback of any kind" in text
    assert "write back" in text
    assert "open channel" not in text and "I'm Tom" not in text


def test_no_em_dashes_anywhere_in_the_copy():
    """The product ruling covers everything a person receives, not only what
    a trader writes."""
    for cadence in ("daily", "weekly", "off"):
        subject, text = compose("Ballast", cadence)
        assert "—" not in subject and "—" not in text


def test_the_reply_ask_is_present():
    """The open feedback channel IS this email; without the ask it is only
    an announcement."""
    _, text = compose("Ballast")
    assert "answer every reply" in text


def test_the_welcome_never_reports_a_trade():
    """It must be true whether or not the trader has traded yet, so it may
    not claim anything about the record."""
    for cadence in ("daily", "off"):
        _, text = compose("Ballast", cadence)
        assert "bought" not in text.lower() and "sold" not in text.lower()


def test_the_welcome_stays_short():
    """v2 ruling: the first draft was cut roughly in half because it read as
    too much text. This ceiling holds the line against it growing back."""
    _, text = compose("A trader with the longest plausible name")
    assert len(text.encode()) < 1200


# ---------------------------------------------------------------- the envelope

def test_it_comes_from_the_founder_not_the_trader():
    msg = envelope("Ballast", "daily", "p@example.com")
    assert msg["from"] == SENDER
    assert "tom@conviction-league.com" in msg["from"]
    assert msg["to"] == ["p@example.com"]


def test_no_bulk_mail_headers():
    """Transactional, not bulk: no unsubscribe — there is no subscription to
    leave. The trader's letters carry their own."""
    msg = envelope("Ballast", "daily", "p@example.com")
    assert "headers" not in msg


def test_the_plain_text_alternative_is_mandatory():
    msg = envelope("Ballast", "daily", "p@example.com")
    assert msg["text"] and msg["html"]


# --------------------------------------------------------------- the hierarchy

def test_the_html_says_the_same_words_as_the_text():
    """Both renderings are assembled from the same beat strings; this holds
    the door shut on them drifting apart."""
    import html
    import re

    _, text = compose("Ballast")
    page = html.unescape(re.sub(r"<[^>]+>", "\n", render_html("Ballast")))
    for line in text.strip().splitlines():
        line = line.strip()
        if line and "conviction-league.com/desk" not in line:
            assert line in page, f"html lost: {line!r}"


def test_the_disclaimer_is_small_print_and_the_ask_steps_back():
    """The v7 ruling: flat text made everything equally loud. The body speaks
    at 16px ink; the feedback ask is smaller and greyer; the disclaimer is
    smaller and greyer still."""
    page = render_html("Ballast")
    from jobs.letter import C
    from jobs.welcome import DISCLAIMER, FEEDBACK
    disclaimer = next(p for p in page.split("<p") if DISCLAIMER in p)
    ask = next(p for p in page.split("<p") if FEEDBACK in p)
    assert "11.5px" in disclaimer and C["muted"] in disclaimer
    assert "13.5px" in ask and C["ink2"] in ask


def test_still_a_personal_note_not_a_designed_email():
    """No images, no buttons, one link: the desk."""
    page = render_html("Ballast")
    assert "<img" not in page
    assert page.count("<a ") == 1 and f"{SITE}/desk" in page


def test_a_traders_name_cannot_break_the_markup():
    assert "<script>" not in render_html("<script>alert(1)</script>")


# ------------------------------------------------------------------- the guard

class _Conn:
    def __init__(self, found=False):
        self.found, self.sql, self.params = found, "", ()
        self.kept, self.committed = [], 0

    def execute(self, sql, params):
        if sql == LETTER_INSERT:
            self.kept.append(params)
        else:
            self.sql, self.params = sql, params
        return self

    def fetchone(self):
        return {"?column?": 1} if self.found else None

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass


def test_one_welcome_per_trader_ever():
    conn = _Conn(found=True)
    assert already_welcomed(conn, "ballast") is True
    assert "occasion = 'welcome'" in conn.sql
    assert "decision = 'sent'" in conn.sql
    assert "interval" not in conn.sql  # ever means ever, not this week


# ---------------------------------------------------------------- the outcomes

class _Snap:
    def __init__(self, email):
        self.exists = email is not None
        self._email = email

    def to_dict(self):
        return {"email": self._email}


class _FS:
    """users/{uid} with one address on file."""

    def __init__(self, email="p@example.com"):
        self._email = email

    def collection(self, name):
        return self

    def document(self, uid):
        return self

    def get(self):
        return _Snap(self._email)


def _sent_ok(msg, key):
    return 200, {"id": "re_welcome_1"}


def test_a_seating_welcome_is_sent_and_recorded():
    conn = _Conn()
    got = send_welcome(conn, _FS(), "ballast", "Ballast", "daily", "uid1",
                       send=True, key="k", deliver_fn=_sent_ok, day="Jul 30")
    assert got == "sent"
    row = conn.kept[0]
    assert row[0] == "ballast" and row[2] == "welcome" and row[3] == "sent"
    assert row[7] == "re_welcome_1"  # the receipt
    assert row[6] == "uid1"          # who was written to — never the address


def test_an_already_welcomed_trader_is_skipped_before_composing():
    conn = _Conn(found=True)
    got = send_welcome(conn, _FS(), "ballast", "Ballast", "daily", "uid1",
                       send=True, key="k", deliver_fn=_sent_ok)
    assert got == "skipped"
    assert conn.kept == []  # the first row stands alone


def test_again_overrides_the_guard_for_copy_iteration():
    """CLI-only escape hatch; ingest never passes it."""
    conn = _Conn(found=True)
    got = send_welcome(conn, _FS(), "ballast", "Ballast", "daily", "uid1",
                       send=True, key="k", deliver_fn=_sent_ok, again=True,
                       day="Jul 30")
    assert got == "sent"


def test_no_address_on_file_is_quiet_and_says_so():
    conn = _Conn()
    got = send_welcome(conn, _FS(email=None), "ballast", "Ballast", "daily",
                       "uid1", send=True, key="k", deliver_fn=_sent_ok)
    assert got == "quiet"
    assert conn.kept[0][3] == "quiet"


def test_a_dry_run_delivers_nothing_but_is_recorded():
    delivered = []

    def spy(msg, key):
        delivered.append(msg)
        return 200, {}

    conn = _Conn()
    got = send_welcome(conn, _FS(), "ballast", "Ballast", "daily", "uid1",
                       send=False, deliver_fn=spy)
    assert got == "dry" and delivered == []
    assert conn.kept[0][3] == "dry"


def test_a_delivery_failure_never_raises_into_the_seating():
    """The seat is the product; the email is the courtesy."""

    def boom(msg, key):
        raise RuntimeError("resend refused (500)")

    conn = _Conn()
    got = send_welcome(conn, _FS(), "ballast", "Ballast", "daily", "uid1",
                       send=True, key="k", deliver_fn=boom)
    assert got == "failed"
    assert conn.kept[0][3] == "failed"
    assert "resend refused" in conn.kept[0][11]
