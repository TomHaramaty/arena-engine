"""The nudge: who is owed one, what it says, and the once-ever guard.

The failure this file exists to prevent is not a broken letter. It is a letter
that reaches the wrong person: someone who already has a trader, someone who
typed two sentences and left, or someone who has had this exact message once
already.
"""

from datetime import datetime, timedelta, timezone

import pytest

from jobs import nudge
from jobs.nudge import (
    ONE_STEP,
    UNFINISHED,
    already_nudged,
    archive_row,
    candidates,
    compiled_something,
    compose,
    due,
    envelope,
    first_name,
    render_html,
    replay_draft,
    send_nudge,
    subject_of,
    trader_name,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def model(raw):
    return {"role": "model", "raw": raw}


def side(**draft):
    import json
    return model("Some words for the principal.\n```json\n"
                 + json.dumps({"draft": draft}) + "\n```")


def interview(*, hours_ago=48.0, done=False, ready=False, history=None):
    return {"done": done, "ready": ready, "history": history or [],
            "updatedAt": NOW - timedelta(hours=hours_ago)}


# ------------------------------------------------------- replaying the mirror

def test_the_charter_is_rebuilt_from_the_side_channel_in_order():
    """The same replay the seat page does: later turns win, so a name chosen
    and then changed reads as the change."""
    draft = replay_draft([side(credo="Buy what the crowd forgot."),
                          {"role": "user", "raw": "call it Nexus"},
                          side(name="pilot"), side(name="nexus")])
    assert draft["name"] == "nexus"
    assert draft["credo"] == "Buy what the crowd forgot."


def test_a_users_own_words_are_never_read_as_a_draft():
    """A principal can type a JSON fence; it is not a side channel."""
    assert replay_draft([{"role": "user", "raw": '```json\n{"draft":{"name":"mine"}}\n```'}]) == {}


def test_a_half_written_reply_takes_nothing_away():
    """A truncated fence must not undo a field an earlier turn established."""
    draft = replay_draft([side(name="nexus"), model("thinking...\n```json\n{\"dra")])
    assert draft["name"] == "nexus"


def test_a_turn_with_no_fence_is_simply_skipped():
    assert replay_draft([model("no side channel here"), side(name="nexus")])["name"] == "nexus"


# ---------------------------------------------------------------- the trader

def test_an_unnamed_interview_is_not_given_a_name():
    assert trader_name({}) == ""
    assert trader_name({"name": "   "}) == ""


def test_a_name_that_could_not_be_a_name_is_refused():
    assert trader_name({"name": "x" * 60}) == ""
    assert trader_name({"name": "two\nlines"}) == ""


def test_compiled_means_the_registrar_wrote_something_down():
    assert compiled_something({"credo": "Cut losers early."}) is True
    assert compiled_something({"principles": [{"text": "P1"}]}) is True
    assert compiled_something({"principles": []}) is False
    assert compiled_something({}) is False


# ----------------------------------------------------------------- who is owed

def test_a_finished_interview_that_was_never_countersigned_is_the_whole_point():
    occasion, why = due(interview(done=True, ready=True, hours_ago=5), NOW)
    assert occasion == ONE_STEP and "untouched" in why


def test_someone_still_in_the_room_is_left_alone():
    """Three hours, not three minutes: a principal who stepped away for coffee
    must not be emailed about the page they are about to come back to."""
    occasion, why = due(interview(done=True, ready=True, hours_ago=1), NOW)
    assert occasion == "" and "still warm" in why


def test_an_interview_put_down_partway_waits_a_day():
    warm = interview(history=[side(credo="Ride trends.")], hours_ago=6)
    assert due(warm, NOW)[0] == ""
    cold = interview(history=[side(credo="Ride trends.")], hours_ago=30)
    assert due(cold, NOW)[0] == UNFINISHED


def test_two_sentences_and_a_closed_tab_is_not_a_person_we_interrupted():
    """Nothing was compiled, so there is nothing waiting for them and no
    letter. This is the live case of the principal who signed up and left
    after two turns."""
    occasion, why = due(interview(history=[model("Welcome. What annoys you?")],
                                  hours_ago=200), NOW)
    assert occasion == "" and why == "nothing compiled yet"


def test_a_finished_interview_needs_no_compiled_check():
    """done+ready means the charter validated; the flags are the evidence."""
    assert due(interview(done=True, ready=True, hours_ago=99), NOW)[0] == ONE_STEP


def test_half_finished_flags_are_not_finished():
    """done without ready is the model closing its own file early. It is
    treated as an unfinished interview, not as one click from the floor."""
    doc = interview(done=True, ready=False, history=[side(credo="x")], hours_ago=48)
    assert due(doc, NOW)[0] == UNFINISHED


def test_an_interview_with_no_timestamp_is_old_enough():
    doc = {"done": True, "ready": True, "history": []}
    assert due(doc, NOW)[0] == ONE_STEP


def test_a_broken_timestamp_does_not_crash_the_run():
    doc = {"done": True, "ready": True, "history": [], "updatedAt": "not a date"}
    assert due(doc, NOW)[0] == ONE_STEP


# --------------------------------------------------------------- the exclusion

class FakeDoc:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data

    def to_dict(self):
        return self._data


class FakeColl:
    def __init__(self, docs):
        self._docs = docs

    def stream(self):
        return list(self._docs)


class FakeFS:
    def __init__(self, drafts=None, applications=None, users=None):
        self._c = {"drafts": FakeColl(drafts or []),
                   "applications": FakeColl(applications or [])}
        self.users = users or {}

    def collection(self, name):
        if name == "users":
            self._pending = "users"
            return self
        return self._c[name]

    def document(self, uid):
        self._uid = uid
        return self

    def get(self):
        data = self.users.get(self._uid)

        class Snap:
            exists = data is not None

            def to_dict(self_inner):
                return data

        return Snap()


class FakeConn:
    """Answers the two questions this job asks, and refuses anything else so a
    silent 'no rows' can never stand in for a query nobody wrote."""

    def __init__(self, owner_uids=(), nudged=()):
        self.owner_uids = list(owner_uids)
        self.nudged = set(nudged)
        self.kept, self.commits, self.rollbacks = [], 0, 0
        self._last, self.sql = None, ""

    def execute(self, sql, params=None):
        s = self.sql = " ".join(sql.split())
        if s.startswith("select owner_uid from agents"):
            self._last = [{"owner_uid": u} for u in self.owner_uids]
        elif s.startswith("select 1 from nudges"):
            self._last = [{"?column?": 1}] if tuple(params) in self.nudged else []
        elif s.startswith("insert into nudges"):
            self.kept.append(params)
            self._last = []
        else:
            raise AssertionError(f"unexpected sql: {s}")
        return self

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_a_principal_who_already_has_a_trader_is_never_nudged():
    """Their application was judged. This letter would be nonsense to them,
    and the exclusion covers a rejected application too."""
    fs = FakeFS(drafts=[FakeDoc("seated-uid", interview(done=True, ready=True))],
                applications=[FakeDoc("app1", {"uid": "seated-uid", "status": "seated"})])
    assert candidates(fs, FakeConn(), NOW) == []

    fs = FakeFS(drafts=[FakeDoc("rejected-uid", interview(done=True, ready=True))],
                applications=[FakeDoc("app1", {"uid": "rejected-uid", "status": "rejected"})])
    assert candidates(fs, FakeConn(), NOW) == []


def test_an_owner_of_a_live_agent_is_never_nudged_even_with_no_application():
    """The agents table is the second gate: a house-seated or hand-seated
    trader has no application doc to exclude on."""
    fs = FakeFS(drafts=[FakeDoc("owner", interview(done=True, ready=True))])
    assert candidates(fs, FakeConn(owner_uids=["owner"]), NOW) == []


def test_the_owed_carry_their_occasion_and_their_traders_name():
    fs = FakeFS(drafts=[
        FakeDoc("finished", interview(done=True, ready=True, hours_ago=9,
                                      history=[side(name="nexus")])),
        FakeDoc("partway", interview(hours_ago=48, history=[side(credo="Ride trends.")])),
        FakeDoc("warm", interview(done=True, ready=True, hours_ago=0.2)),
    ])
    owed = {c["uid"]: c for c in candidates(fs, FakeConn(), NOW)}
    assert set(owed) == {"finished", "partway"}
    assert owed["finished"]["occasion"] == ONE_STEP
    assert owed["finished"]["trader"] == "nexus"
    assert owed["partway"]["occasion"] == UNFINISHED
    assert owed["partway"]["trader"] == ""


# -------------------------------------------------------------------- the copy

def test_it_names_the_trader_when_the_interview_named_one():
    _, text = compose(ONE_STEP, "Ron", "Nexus")
    assert "Hi Ron," in text and "Nexus" in text


def test_an_unnamed_trader_is_never_given_a_name():
    subject, text = compose(UNFINISHED, "Peleg", "")
    assert "your trader" in text
    assert subject == "Your interview is still where you left it"


def test_the_finished_letter_asks_for_the_one_thing_missing():
    _, text = compose(ONE_STEP, "Ron", "Nexus")
    assert "countersign" in text.lower()
    assert "next bell" in text


def test_the_unfinished_letter_never_says_it_is_finished():
    _, text = compose(UNFINISHED, "Peleg", "")
    assert "countersign" not in text.lower()
    assert "put it down partway" in text


def test_both_letters_promise_that_nothing_needs_redoing():
    """The single most important sentence: the reason people do not come back
    is that they assume the fifteen minutes are gone."""
    for occasion in (ONE_STEP, UNFINISHED):
        _, text = compose(occasion, "Ron", "Nexus")
        assert "Nothing needs redoing" in text
        assert "any device" in text


def test_the_letter_goes_back_to_the_seat_not_the_desk():
    """A principal with no trader has nothing at a desk."""
    for occasion in (ONE_STEP, UNFINISHED):
        _, text = compose(occasion, "Ron", "Nexus")
        assert "conviction-league.com/seat/" in text
        assert "/desk" not in text


def test_it_asks_what_got_in_the_way():
    _, text = compose(UNFINISHED, "Peleg", "")
    assert "write back" in text and "most useful thing you could send." in text


def test_no_greeting_at_all_beats_a_wrong_one():
    _, text = compose(ONE_STEP, "", "Nexus")
    assert text.startswith("Hello, you finished")


def test_the_letter_states_no_quantity_at_all():
    """There is no model in this file and no number in this letter, so there
    is nothing to fabricate. This holds that property in place."""
    import re
    for occasion in (ONE_STEP, UNFINISHED):
        subject, text = compose(occasion, "Ron", "Nexus")
        assert not re.search(r"\d", subject + text.replace("conviction-league.com", ""))


def test_no_em_dashes_anywhere_in_the_copy():
    for occasion in (ONE_STEP, UNFINISHED):
        subject, text = compose(occasion, "Ron", "Nexus")
        assert "—" not in subject and "—" not in text


def test_it_stays_short():
    _, text = compose(ONE_STEP, "Ron", "A trader with the longest plausible name")
    assert len(text.encode()) < 1200


def test_an_unknown_occasion_is_refused_rather_than_composed():
    with pytest.raises(ValueError):
        compose("reminder", "Ron", "Nexus")


# ---------------------------------------------------------------- the envelope

def test_it_comes_from_the_founder_and_carries_no_bulk_headers():
    msg = envelope(ONE_STEP, "Ron", "Nexus", "p@example.com")
    assert msg["from"] == nudge.SENDER and "tom@conviction-league.com" in msg["from"]
    assert msg["to"] == ["p@example.com"]
    assert "headers" not in msg


def test_the_plain_text_alternative_is_mandatory():
    msg = envelope(UNFINISHED, "", "", "p@example.com")
    assert msg["text"] and msg["html"]


def test_the_html_says_the_same_words_as_the_text():
    import html
    import re
    _, text = compose(ONE_STEP, "Ron", "Nexus")
    page = html.unescape(re.sub(r"<[^>]+>", "\n", render_html(ONE_STEP, "Ron", "Nexus")))
    for line in text.strip().splitlines():
        line = line.strip()
        if line and "conviction-league.com/seat" not in line:
            assert line in page, f"html lost: {line!r}"


def test_still_a_personal_note_not_a_designed_email():
    page = render_html(ONE_STEP, "Ron", "Nexus")
    assert "<img" not in page
    assert page.count("<a ") == 1 and f"{nudge.SITE}/seat/" in page


def test_a_traders_name_cannot_break_the_markup():
    assert "<script>" not in render_html(ONE_STEP, "Ron", "<script>alert(1)</script>")
    assert "<script>" not in subject_of(ONE_STEP, "x") + render_html(UNFINISHED, "<b>", "")


def test_an_address_is_never_used_as_a_greeting():
    fs = FakeFS(users={"u": {"email": "klavior@gmail.com"}})
    assert first_name(fs, "u") == ""
    fs = FakeFS(users={"u": {"displayName": "Or Lavi", "email": "klavior@gmail.com"}})
    assert first_name(fs, "u") == "Or"


# ------------------------------------------------------------------- the record

def test_the_row_never_carries_the_address():
    row = archive_row("uid", ONE_STEP, "sent", subject="s", html="<p>hi</p>",
                      plain="hi", provider_id="re_1", trader="nexus")
    assert "@" not in " ".join(str(v) for v in row if v)
    assert row[0] == "uid" and row[1] == ONE_STEP and row[2] == "sent"
    assert row[-2] == len("<p>hi</p>".encode())


def test_an_unknown_decision_or_occasion_cannot_be_written():
    with pytest.raises(ValueError):
        archive_row("uid", ONE_STEP, "posted")
    with pytest.raises(ValueError):
        archive_row("uid", "reminder", "sent")


def test_once_ever_means_no_interval():
    """A window would let the same message arrive again a week later, which is
    the one thing a message whose whole point is arriving once must not do."""
    conn = FakeConn(nudged={("uid", ONE_STEP)})
    assert already_nudged(conn, "uid", ONE_STEP) is True
    assert already_nudged(conn, "uid", UNFINISHED) is False
    assert "interval" not in conn.sql
    assert "decision = 'sent'" in conn.sql


# ------------------------------------------------------------------ the sending

def sent_ok(msg, key, post=None):
    return 200, {"id": "re_123"}


def test_a_second_copy_is_never_sent():
    conn = FakeConn(nudged={("uid", ONE_STEP)})
    fs = FakeFS(users={"uid": {"email": "p@example.com"}})
    assert send_nudge(conn, fs, "uid", ONE_STEP, send=True, key="k",
                      deliver_fn=sent_ok) == "skipped"
    assert conn.kept == []


def test_no_address_means_no_letter_and_a_row_saying_so():
    conn = FakeConn()
    fs = FakeFS(users={})
    assert send_nudge(conn, fs, "uid", ONE_STEP, send=True, key="k",
                      deliver_fn=sent_ok) == "quiet"
    assert conn.kept[0][2] == "quiet" and conn.kept[0][3] == "no address on file"


def test_a_dry_run_records_what_it_would_have_sent_and_sends_nothing():
    conn = FakeConn()
    fs = FakeFS(users={"uid": {"email": "p@example.com", "displayName": "Ron Famini"}})

    def refuse(msg, key, post=None):
        raise AssertionError("a dry run must not deliver")

    assert send_nudge(conn, fs, "uid", ONE_STEP, "nexus", deliver_fn=refuse) == "dry"
    row = conn.kept[0]
    assert row[2] == "dry" and "nexus" in row[4].lower() and row[5] == "nexus"


def test_a_sent_letter_keeps_the_receipt():
    conn = FakeConn()
    fs = FakeFS(users={"uid": {"email": "p@example.com", "displayName": "Ron Famini"}})
    assert send_nudge(conn, fs, "uid", ONE_STEP, "nexus", send=True, key="k",
                      deliver_fn=sent_ok) == "sent"
    row = conn.kept[0]
    assert row[2] == "sent" and row[6] == "re_123"


def test_a_provider_failure_is_recorded_and_never_raised():
    """This job runs beside the record's own work. It may not take a run down."""
    conn = FakeConn()
    fs = FakeFS(users={"uid": {"email": "p@example.com"}})

    def boom(msg, key, post=None):
        raise RuntimeError("resend refused (403)")

    assert send_nudge(conn, fs, "uid", ONE_STEP, send=True, key="k",
                      deliver_fn=boom) == "failed"
    assert conn.kept[0][2] == "failed" and "403" in conn.kept[0][-1]


def test_the_greeting_survives_a_principal_with_no_display_name():
    conn = FakeConn()
    fs = FakeFS(users={"uid": {"email": "p@example.com"}})
    assert send_nudge(conn, fs, "uid", UNFINISHED, send=True, key="k",
                      deliver_fn=sent_ok) == "sent"
    assert conn.kept[0][8].startswith("Hello,")
