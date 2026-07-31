"""The nudge — one letter to a principal who never took their seat.

Design: trader repo design/nudge-2026-07-31.md.

A finished interview that is never countersigned produces nothing and tells
nobody. Ron Famini's sat for two days and the only reason anyone knew was that
he wrote in to say his trader had disappeared. At eight accounts every one of
those is worth recovering by hand; at ten times this the losses are invisible.

Two kinds of person are addressed here, and the difference matters:

  one-step   the interview is finished, the charter validates, the tape was
             read. All that is missing is the countersign. Real intent, and a
             minute of their time recovers it.
  unfinished the interview was put down partway with something already
             compiled. Softer, later, and never sent to someone who typed two
             sentences and left, because that is not a person we interrupted.

THREE LAWS, all of them about not writing to the wrong person:

1. No trader, no nudge in the other direction: a uid with any application on
   file (seated, submitted, even rejected) is out of scope. Their charter has
   already been judged, and this letter would be nonsense to them.
2. Once per person per occasion, ever. Enforced by the nudges table's partial
   unique index, not only by the check in `already_nudged`.
3. Nothing is invented. There is no model call anywhere in this file. The
   trader's name comes from the interview's own side channel, replayed the way
   the seat page replays it, and when it has not been chosen yet the letter
   simply does not name one.

Like jobs/letter.py and jobs/welcome.py, this is the founder's channel and
sending is opt-in: the CLI is dry by default, and the daily job passes --send
only from CI. These are other people's inboxes.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
from datetime import datetime, timezone

from jobs.letter import C, MONO, SERIF, deliver, recipient

SITE = "https://conviction-league.com"
SENDER = "Tom · Conviction League <tom@conviction-league.com>"

ONE_STEP = "one-step"
UNFINISHED = "unfinished"
OCCASIONS = (ONE_STEP, UNFINISHED)

#: How long an interview must lie untouched before it counts as put down. The
#: finished one waits hours rather than minutes because a principal who is
#: still in the room does not need a letter about the room; the unfinished one
#: waits a day because stopping halfway is often just an interruption.
QUIET_HOURS = {ONE_STEP: 3, UNFINISHED: 24}

#: The seat page's side channel: the last fenced JSON object of a model turn
#: (seat/app.js parseSideChannel). Replaying these in order is how the charter
#: survives a closed tab, and it is the only place a trader's name exists
#: before an application is written.
FENCE = re.compile(r"```json\s*([\s\S]*?)```")


# --------------------------------------------------------------- the interview

def replay_draft(history) -> dict:
    """The charter as the seat page would rebuild it from the mirror.

    Merges the `draft` of every model turn's side channel in order, so the
    last word on any field wins. A turn without a fence, or with one that does
    not parse, is skipped exactly as the client skips it: a half-written reply
    must never take a field away that an earlier one established.
    """
    draft: dict = {}
    for turn in history or []:
        if not isinstance(turn, dict) or turn.get("role") != "model":
            continue
        blocks = FENCE.findall(str(turn.get("raw") or ""))
        if not blocks:
            continue
        try:
            side = json.loads(blocks[-1])
        except Exception:
            continue
        if isinstance(side, dict) and isinstance(side.get("draft"), dict):
            draft.update(side["draft"])
    return draft


def trader_name(draft: dict) -> str:
    """The name the interview settled on, or nothing.

    Naming happens late in Act II, so most abandoned interviews have no name
    at all. An unnamed trader is written about as 'your trader'; inventing one
    would be putting words in the principal's mouth about the one thing that
    is most theirs.
    """
    name = str((draft or {}).get("name") or "").strip()
    return name if 0 < len(name) <= 40 and "\n" not in name else ""


def compiled_something(draft: dict) -> bool:
    """Did the interview reach anything the principal would recognise as work?

    A credo or a principle means the Registrar had compiled their words into
    the charter. Two turns and a closed tab means it had not, and that person
    is left alone.
    """
    if str((draft or {}).get("credo") or "").strip():
        return True
    return bool([p for p in ((draft or {}).get("principles") or []) if p])


def _age_hours(updated, now) -> float:
    """Hours since the mirror was last written, or a very large number when
    the timestamp is missing: an interview we cannot date is one nobody has
    touched in this run's memory, and the quiet period is a floor on haste,
    not a licence to write to someone whose clock we lost."""
    if not updated:
        return float("inf")
    if isinstance(updated, str):
        try:
            updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            return float("inf")
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (now - updated).total_seconds() / 3600.0


def due(doc: dict, now: datetime, draft: dict | None = None) -> tuple:
    """(occasion, why) for one drafts/{uid} document. Occasion '' means leave
    them alone, and `why` always says which rule decided it.

    Pure and total: every path returns a reason, so a run can print why each
    person was passed over without anyone reading this file to find out.
    """
    draft = replay_draft(doc.get("history")) if draft is None else draft
    finished = bool(doc.get("done")) and bool(doc.get("ready"))
    occasion = ONE_STEP if finished else UNFINISHED
    if not finished and not compiled_something(draft):
        return "", "nothing compiled yet"
    age = _age_hours(doc.get("updatedAt"), now)
    wait = QUIET_HOURS[occasion]
    if age < wait:
        return "", f"touched {age:.1f}h ago, still warm"
    return occasion, f"{occasion}, untouched {age:.0f}h"


def candidates(fs, conn, now=None) -> list:
    """Every principal owed a letter, newest interview first.

    A uid with any application on file is excluded here rather than later:
    their charter has been judged, and the exclusion is about who these
    letters are for, not about whether one has been sent yet.
    """
    now = now or datetime.now(timezone.utc)
    claimed = {str((d.to_dict() or {}).get("uid") or "")
               for d in fs.collection("applications").stream()}
    claimed |= {r["owner_uid"] for r in conn.execute(
        "select owner_uid from agents where owner_uid is not null").fetchall()}
    out = []
    for doc in fs.collection("drafts").stream():
        uid = doc.id
        data = doc.to_dict() or {}
        if uid in claimed:
            continue
        draft = replay_draft(data.get("history"))
        occasion, why = due(data, now, draft)
        if not occasion:
            print(f"  {uid}: no letter — {why}")
            continue
        out.append({"uid": uid, "occasion": occasion, "why": why,
                    "trader": trader_name(draft)})
    return out


# ------------------------------------------------------------------- the copy
#
# One named beat per paragraph, and both renderings are assembled from these
# same strings, so the HTML and the plain text can never say different things.
# The same law as the welcome. No numbers appear anywhere in this letter, so
# there is nothing here for a quantity guard to catch.

GREETING = "Hi {first},"
GREETING_ANON = "Hello,"

FINISHED = ("you finished chartering {trader} and it is one step from the "
            "floor. The last thing it needs is your countersign.")

STOPPED = ("you started chartering {trader} and put it down partway. It is "
           "still exactly where you left it.")

#: The hook, and the beat this letter is really for. What is waiting on the
#: other side of the click is not a form, it is a trader that runs without
#: them: both bells, every market day, by their rules, with its reasoning
#: written down where they can read it. Every clause here is true of every
#: seated trader, so the letter promises nothing the floor does not do.
AUTONOMY_FINISHED = (
    "That takes a minute. After it, {trader} runs on its own: it takes its "
    "seat at the next bell, trades by the rules you wrote, and writes down "
    "its reasoning where you can read it."
)

AUTONOMY_STOPPED = (
    "A few more minutes finishes it. After that {trader} runs on its own: it "
    "trades at both bells every market day, by the rules you set, and writes "
    "down its reasoning where you can read it."
)

SAVED = ("Everything you said is saved. Nothing needs redoing: sign in on any "
         "device and it opens on the question you stopped at.")

SAVED_FINISHED = ("Everything you said is saved. Nothing needs redoing: sign "
                  "in on any device, read it once more, and countersign.")

ASK = ("If something got in the way, or the interview asked you for more than "
       "it should have, write back and tell me. That is the most useful thing "
       "you could send.")

DISCLAIMER = ("Every fill is simulated: real prices, no real money, not "
              "investment advice.")

LINK = {ONE_STEP: "Countersign and finish", UNFINISHED: "Pick it up where you left off"}

SUBJECTS = {
    ONE_STEP: "{Trader} is one step from the floor",
    UNFINISHED: "Your interview is still where you left it",
}


def _trader(trader: str) -> str:
    return trader if trader else "your trader"


def subject_of(occasion: str, trader: str) -> str:
    line = SUBJECTS[occasion].format(Trader=_trader(trader))
    return line[0].upper() + line[1:]


def _beats(occasion: str, first: str, trader: str) -> list:
    """Greeting and where they stopped, then the hook, then the reassurance.

    The hook sits second on purpose: the reason to come back is what the
    trader does afterwards, and 'nothing was lost' only matters to someone who
    has already decided to finish.
    """
    who = _trader(trader)
    finished = occasion == ONE_STEP
    opening = (FINISHED if finished else STOPPED).format(trader=who)
    return [(GREETING.format(first=first) if first else GREETING_ANON) + " " + opening,
            (AUTONOMY_FINISHED if finished else AUTONOMY_STOPPED).format(trader=who),
            SAVED_FINISHED if finished else SAVED]


def compose(occasion: str, first: str = "", trader: str = "") -> tuple:
    """Subject and plain-text body. Pure, so the copy is testable."""
    if occasion not in OCCASIONS:
        raise ValueError(f"unknown occasion {occasion!r}; expected one of {OCCASIONS}")
    text = "\n\n".join(_beats(occasion, first, trader)
                       + [f"    {SITE}/seat/", ASK, "Tom", DISCLAIMER])
    return subject_of(occasion, trader), text + "\n"


def render_html(occasion: str, first: str = "", trader: str = "") -> str:
    """The same words with a hierarchy, in the welcome's clothes: house paper,
    serif, one link, no images, no buttons. A person writing, not a funnel."""
    e = _html.escape
    para = (f'<p style="margin:0 0 18px;font:400 16px/1.65 {SERIF};'
            f'color:{C["ink"]};">{{}}</p>')
    beats = "".join(para.format(e(b)) for b in _beats(occasion, first, trader))
    return f"""<div style="background:{C['mat']};padding:36px 16px;">
<div style="max-width:560px;margin:0 auto;background:{C['card']};border:1px solid {C['rule']};padding:38px 42px 30px;">
{beats}<p style="margin:0 0 26px;font:400 16px/1.65 {SERIF};">
<a href="{SITE}/seat/" style="color:{C['brass']};text-decoration:underline;">{e(LINK[occasion])}</a></p>
<p style="margin:0 0 26px;font:400 13.5px/1.7 {SERIF};color:{C['ink2']};">{e(ASK)}</p>
<p style="margin:0 0 26px;font:400 16px/1.65 {SERIF};color:{C['ink']};">Tom</p>
<div style="height:1px;background:{C['rule']};font-size:0;line-height:0;">&nbsp;</div>
<p style="margin:14px 0 0;font:400 11.5px/1.7 {MONO};color:{C['muted']};">{e(DISCLAIMER)}</p>
</div>
<div style="max-width:560px;margin:0 auto;font:400 11px/1.6 {MONO};color:#a9a7a0;padding-top:14px;">conviction-league.com</div>
</div>"""


def envelope(occasion: str, first: str, trader: str, to: str) -> dict:
    """From the founder, no bulk-mail headers: this is a one-time message
    about something the recipient started, not a subscription. Replies go
    where they appear to go."""
    subject, text = compose(occasion, first, trader)
    return {"from": SENDER, "to": [to], "subject": subject,
            "html": render_html(occasion, first, trader), "text": text}


def first_name(fs, uid: str) -> str:
    """The name they signed in with, first word only, or nothing.

    Google hands us a display name; a principal who signed in by email link
    may have none. An address is never used as a greeting: 'Hi klavior' is
    worse than no greeting at all.
    """
    snap = fs.collection("users").document(uid).get()
    data = (snap.to_dict() or {}) if snap is not None and snap.exists else {}
    name = re.sub(r"\s+", " ", str(data.get("displayName") or "")).strip()
    first = name.split(" ")[0] if name else ""
    return first if first.isalpha() and len(first) <= 30 else ""


# ----------------------------------------------------------------- the record

DECISIONS = ("sent", "quiet", "failed", "dry")

NUDGE_INSERT = (
    "insert into nudges (owner_uid, occasion, decision, reason, subject, "
    "trader, provider_id, html, plain, bytes, error) "
    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def archive_row(uid, occasion, decision, *, reason="", subject="", trader="",
                provider_id="", html="", plain="", error=""):
    """The columns of one nudge's record, in `NUDGE_INSERT` order. Pure, so
    the shape of what gets written is assertable without a database. The
    address is not a parameter and cannot be recorded by accident."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}; expected one of {DECISIONS}")
    if occasion not in OCCASIONS:
        raise ValueError(f"unknown occasion {occasion!r}; expected one of {OCCASIONS}")
    return (uid, occasion, decision, reason or None, subject or None,
            trader or None, provider_id or None, html or None, plain or None,
            len(html.encode("utf-8")) if html else None, error or None)


def already_nudged(conn, uid, occasion) -> bool:
    """Has this person had this letter? Ever means ever: no interval, no
    second copy of a message whose whole point was that it arrives once."""
    row = conn.execute(
        "select 1 from nudges where owner_uid = %s and occasion = %s "
        "and decision = 'sent' limit 1",
        (uid, occasion),
    ).fetchone()
    return row is not None


def send_nudge(conn, fs, uid, occasion, trader="", *, send=False, key="",
               out=None, deliver_fn=deliver, again=False):
    """Write to one principal. Returns 'sent', 'dry', 'quiet', 'skipped' or
    'failed', and never raises: this job runs beside the record's own work and
    a letter must not be able to disturb it.

    Every outcome except 'skipped' is recorded. `again` overrides the once-ever
    guard so the operator can iterate on the copy against their own inbox; it
    is reachable only from the CLI, with --only.
    """
    def keep(decision, **fields):
        try:
            conn.execute(NUDGE_INSERT,
                         archive_row(uid, occasion, decision, trader=trader, **fields))
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  nudge {uid}: NOT RECORDED — {e}")

    try:
        if already_nudged(conn, uid, occasion) and not again:
            print(f"  nudge {uid}: already written to about {occasion} — once is once")
            return "skipped"
        to = recipient(fs, uid)
        if not to:
            print(f"  nudge {uid}: no address on file — not sending")
            keep("quiet", reason="no address on file")
            return "quiet"
        msg = envelope(occasion, first_name(fs, uid), trader, to)
        if out:
            os.makedirs(out, exist_ok=True)
            stem = os.path.join(out, f"nudge-{occasion}-{uid[:8]}")
            with open(stem + ".html", "w", encoding="utf-8") as f:
                f.write(msg["html"])
            with open(stem + ".txt", "w", encoding="utf-8") as f:
                f.write(msg["text"])
            print(f"  nudge {uid}: written to {stem}.html")
        if not send:
            print(f"  nudge {uid}: WOULD SEND to {to} — {msg['subject']!r}")
            keep("dry", reason=occasion, subject=msg["subject"],
                 html=msg["html"], plain=msg["text"])
            return "dry"
        status, body = deliver_fn(msg, key or os.environ.get("RESEND_API_KEY", ""))
        print(f"  nudge {uid}: sent to {to} — {status} {body.get('id', '')}")
        keep("sent", reason=occasion, subject=msg["subject"],
             provider_id=body.get("id", ""), html=msg["html"], plain=msg["text"])
        return "sent"
    except Exception as e:
        print(f"  nudge {uid}: failed — {e}")
        keep("failed", error=str(e))
        return "failed"


def main():  # pragma: no cover - orchestration, exercised end to end
    """Dry by default, exactly like jobs.letter and jobs.welcome: nothing
    leaves the machine without --send."""
    import argparse

    from engine import db
    from engine import observability as obs
    from jobs.ingest import fs_client

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="restrict to one principal uid")
    ap.add_argument("--send", action="store_true",
                    help="actually deliver; without it nothing leaves the machine")
    ap.add_argument("--out", help="write each composed letter to this directory")
    ap.add_argument("--again", action="store_true",
                    help="override the once-ever guard, for iterating on the "
                         "copy against your own inbox; use with --only")
    args = ap.parse_args()

    obs.init("nudge")
    fs = fs_client()
    conn = db.connect()
    db.migrate(conn)
    owed = candidates(fs, conn)
    if args.only:
        owed = [c for c in owed if c["uid"] == args.only]
    print(f"interviews owed a letter: {len(owed)}")

    outcomes = {}
    for c in owed:
        print(f"  {c['uid']}: {c['why']}"
              + (f" · {c['trader']}" if c["trader"] else " · unnamed"))
        got = send_nudge(conn, fs, c["uid"], c["occasion"], c["trader"],
                         send=args.send, out=args.out,
                         again=args.again and bool(args.only))
        outcomes[got] = outcomes.get(got, 0) + 1
    conn.close()
    print("nudge: " + (", ".join(f"{v} {k}" for k, v in sorted(outcomes.items())) or "nothing to send")
          + ("" if args.send else "  (dry — pass --send to deliver)"))


if __name__ == "__main__":
    main()
