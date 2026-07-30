"""The welcome — the founder's one letter.

Design: trader repo design/welcome-email-2026-07-30.md.

The interview is the most invested moment a principal ever has with the
product, and under ruling 1 of the letters design it is followed by silence
until something happens on the record — days, or for a patient charter, weeks.
This is the one message that always sends, exactly once, at seating: the
guaranteed first touch, and the only place the coming quiet can be framed as
design before it happens.

It is the founder's channel, not the trader's. The trader's letters speak only
about the record; this speaks about the product, asks for replies, and never
reports a trade — by the time it lands the trader may not have traded yet, and
the welcome must be true either way.

Plain text on purpose: at this volume a designed welcome reads as a funnel,
three short paragraphs read as a founder who noticed. No unsubscribe header —
a one-time receipt for an action the principal just took is not a
subscription; the letters carry their own.

Compose and decide are pure; sending is injected. Same law as jobs/letter.py:
nothing sends by accident — the CLI is dry by default, and the ingest call
sends only in CI.
"""

from __future__ import annotations

import html as _html

from jobs.letter import C, LETTER_INSERT, MONO, SERIF, archive_row, deliver, recipient

SITE = "https://conviction-league.com"
SENDER = "Tom · Conviction League <tom@conviction-league.com>"

SUBJECT = "Welcome to Conviction League"

# The copy, one named beat per paragraph. Both renderings are assembled from
# these same strings, so the HTML and the plain text can never say different
# things.

OPENING = "Welcome to Conviction League. We're excited to have you on the floor."

SEATED = "{name} is trading on its own now, by the charter you wrote."

DESK = ("Your desk shows everything: its positions, its watchlist, and the "
        "reasoning behind every move. You can chat with {name} there any "
        "time, and coach it as it learns.")

#: The paragraph this email exists for. A principal who chose cadence 'off'
#: at the interview declined the trader's letters, so promising them would be
#: false — they get told the truth about that instead.
EXPECTATION = (
    "{name} writes to you when something happens. A quiet inbox just means "
    "it is waiting for its moment."
)

EXPECTATION_OFF = (
    "You chose not to get letters from {name}, so this is the only email you "
    "will get. You can turn letters on from your desk any time."
)

FEEDBACK = ("We appreciate questions, ideas, feedback of any kind: just "
            "write back to this email address. We answer every reply.")

DISCLAIMER = ("Every fill is simulated: real prices, no real money, not "
              "investment advice.")


def _beats(name: str, cadence: str) -> list:
    beat = EXPECTATION_OFF if (cadence or "").lower() == "off" else EXPECTATION
    return [OPENING, SEATED.format(name=name), DESK.format(name=name),
            beat.format(name=name)]


def compose(name: str, cadence: str = "daily") -> tuple:
    """Subject and plain-text body. Pure, so the copy is testable."""
    text = "\n\n".join(_beats(name, cadence)
                       + [f"    {SITE}/desk", FEEDBACK, "Tom", DISCLAIMER])
    return SUBJECT, text + "\n"


def render_html(name: str, cadence: str = "daily") -> str:
    """The same words with a hierarchy: the body speaks at full weight, the
    feedback ask steps back, the disclaimer is small print. Deliberately still
    a personal note rather than a designed email — house paper and serif, one
    link, no images, no buttons — so it keeps reading as a person writing.
    """
    e = _html.escape
    para = (f'<p style="margin:0 0 18px;font:400 16px/1.65 {SERIF};'
            f'color:{C["ink"]};">{{}}</p>')
    beats = "".join(para.format(e(b)) for b in _beats(name, cadence))
    return f"""<div style="background:{C['mat']};padding:36px 16px;">
<div style="max-width:560px;margin:0 auto;background:{C['card']};border:1px solid {C['rule']};padding:38px 42px 30px;">
{beats}<p style="margin:0 0 26px;font:400 16px/1.65 {SERIF};">
<a href="{SITE}/desk" style="color:{C['brass']};text-decoration:underline;">Open your desk</a></p>
<p style="margin:0 0 26px;font:400 13.5px/1.7 {SERIF};color:{C['ink2']};">{e(FEEDBACK)}</p>
<p style="margin:0 0 26px;font:400 16px/1.65 {SERIF};color:{C['ink']};">Tom</p>
<div style="height:1px;background:{C['rule']};font-size:0;line-height:0;">&nbsp;</div>
<p style="margin:14px 0 0;font:400 11.5px/1.7 {MONO};color:{C['muted']};">{e(DISCLAIMER)}</p>
</div>
<div style="max-width:560px;margin:0 auto;font:400 11px/1.6 {MONO};color:#a9a7a0;padding-top:14px;">conviction-league.com</div>
</div>"""


def envelope(name: str, cadence: str, to: str) -> dict:
    """From the founder, no bulk-mail headers. Replies go where they appear
    to go: straight back to the from address. The plain-text alternative is
    mandatory, same law as the letters."""
    subject, text = compose(name, cadence)
    return {"from": SENDER, "to": [to], "subject": subject,
            "html": render_html(name, cadence), "text": text}


def already_welcomed(conn, agent_id: str) -> bool:
    """Once per trader, ever — read from the letters archive, so the resume
    path in ingest and the hourly retry can both call this blindly."""
    row = conn.execute(
        "select 1 from letters where agent_id = %s and occasion = 'welcome' "
        "and decision = 'sent' limit 1",
        (agent_id,),
    ).fetchone()
    return row is not None


def send_welcome(conn, fs, agent_id, name, cadence, uid, *,
                 send=False, key="", out=None, deliver_fn=deliver, day=None,
                 again=False):
    """Welcome one principal. Returns what happened: 'sent', 'dry', 'quiet',
    'skipped', or 'failed'. Never raises — the seat is the product and the
    email is the courtesy, so a welcome failure must not fail a seating.

    Every outcome except 'skipped' is recorded in the letters archive
    (occasion 'welcome'); skipped means a sent row already exists and the
    first one stands. `again` overrides that guard — it exists so the
    operator can iterate on the copy against a real inbox, it is reachable
    only from the CLI, and ingest never passes it.
    """
    import os
    from datetime import datetime, timezone

    day = day or datetime.now(timezone.utc).strftime("%b %d")

    def keep(decision, **fields):
        try:
            conn.execute(LETTER_INSERT,
                         archive_row(agent_id, day, "welcome", decision, **fields))
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  welcome {agent_id}: NOT RECORDED — {e}")

    try:
        if already_welcomed(conn, agent_id) and not again:
            print(f"  welcome {agent_id}: already welcomed — the first one stands")
            return "skipped"
        to = recipient(fs, uid)
        if not to:
            print(f"  welcome {agent_id}: no address on file — not sending")
            keep("quiet", reason="no address on file", owner_uid=uid)
            return "quiet"
        msg = envelope(name, cadence, to)
        if out:
            os.makedirs(out, exist_ok=True)
            stem = os.path.join(out, f"welcome-{agent_id}")
            with open(stem + ".html", "w", encoding="utf-8") as f:
                f.write(msg["html"])
            with open(stem + ".txt", "w", encoding="utf-8") as f:
                f.write(msg["text"])
            print(f"  welcome {agent_id}: written to {stem}.html")
        if not send:
            print(f"  welcome {agent_id}: WOULD SEND to {to}")
            keep("dry", reason="at seating", subject=msg["subject"],
                 owner_uid=uid, html=msg["html"], plain=msg["text"])
            return "dry"
        status, body = deliver_fn(msg, key or os.environ.get("RESEND_API_KEY", ""))
        print(f"  welcome {agent_id}: sent to {to} — {status} {body.get('id', '')}")
        keep("sent", reason="at seating", subject=msg["subject"], owner_uid=uid,
             provider_id=body.get("id", ""), html=msg["html"], plain=msg["text"])
        return "sent"
    except Exception as e:
        print(f"  welcome {agent_id}: failed — {e}")
        keep("failed", error=str(e), owner_uid=uid)
        return "failed"


def main():  # pragma: no cover - orchestration, exercised end to end
    """Manual welcomes: preview, or send to traders seated before this
    existed. Dry by default, exactly like jobs.letter."""
    import argparse
    import json

    from engine import db
    from jobs.ingest import fs_client

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="restrict to one trader id")
    ap.add_argument("--send", action="store_true",
                    help="actually deliver; without it nothing leaves the machine")
    ap.add_argument("--out", help="write each composed welcome to this directory")
    ap.add_argument("--again", action="store_true",
                    help="override the once-ever guard — for iterating on the "
                         "copy against a real inbox; use with --only")
    args = ap.parse_args()

    fs = fs_client()
    conn = db.connect()
    db.migrate(conn)
    rows = conn.execute(
        "select id, name, config, owner_uid from agents "
        "where status='active' and owner_uid is not null order by id"
    ).fetchall()
    if args.only:
        rows = [r for r in rows if r["id"] == args.only]

    outcomes = {}
    for row in rows:
        cfg = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"] or "{}")
        cadence = (cfg.get("updates") or {}).get("cadence", "daily")
        got = send_welcome(conn, fs, row["id"], row["name"], cadence,
                           row["owner_uid"], send=args.send, out=args.out,
                           again=args.again and bool(args.only))
        outcomes[got] = outcomes.get(got, 0) + 1
    conn.close()
    print("welcome: " + ", ".join(f"{v} {k}" for k, v in sorted(outcomes.items()))
          + ("" if args.send else "  (dry — pass --send to deliver)"))


if __name__ == "__main__":
    main()
