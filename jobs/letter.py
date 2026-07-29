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


#: After this many consecutive silent Fridays, the Friday letter goes out
#: anyway and says so. Ruling 1 means a patient trader can write nothing for
#: weeks — Ballast sits in cash by design until the VIX reaches 25 — and to a
#: principal, silence is indistinguishable from a broken product. Waiting is a
#: real result and gets reported as one rather than hidden.
SILENT_FRIDAYS_BEFORE_SPEAKING = 2


def quiet_weeks(tape: list, agent_id: str, now_t: int) -> int:
    """Whole weeks since this trader last did anything on the record.

    Read from the tape rather than tracked in a table: the tape already IS the
    history of what happened, so nothing can drift out of step with it.
    """
    times = [e.get("t", 0) for e in tape
             if e.get("agent") == agent_id and e.get("event") in EVENT_KINDS]
    if not times:
        return 0
    return max(0, int((now_t - max(times)) // (7 * 86400)))


def eligible(cadence: str, events: list, *, is_reflection_day: bool = False,
             answered_guidance: int = 0, rulebook_changed: bool = False,
             silent_fridays: int = 0) -> tuple:
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
        return True, "weekly: the reflection ran"

    # daily — ruling 1: only on a day something actually happened
    if is_reflection_day:
        return True, "the reflection ran"
    if not happened:
        # ...except that going quiet indefinitely reads as breakage. On a
        # Friday, after enough silence, say so.
        if silent_fridays >= SILENT_FRIDAYS_BEFORE_SPEAKING:
            return True, f"still waiting: {silent_fridays} quiet weeks, and silence is not a status"
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
        # A sandbox trader is published beside the floor, not on it. It can still
        # be sent a letter with --only; the board it reads is the real floor.
        agent = next((a for a in (arena.get("sandbox") or [])
                      if a.get("id") == agent_id), None)
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
            # the voice is the principal's, authored at the interview — the
            # letter speaks in it rather than inventing one
            "voice": (agent.get("charter") or {}).get("voice") or "",
            "credo": (agent.get("charter") or {}).get("credo") or "",
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


# ------------------------------------------------------------------ rendering

SITE = "https://conviction-league.com"

#: House palette, lifted from arena-web web/static/seat/seat.css. Email has no
#: custom properties worth relying on, so the values are inlined.
C = {
    "mat": "#f1f0ec", "card": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e",
    "muted": "#898781", "rule": "#e1e0d9", "tint": "#f5f3ec",
    "brass": "#a06d12", "good": "#006300", "bad": "#d03b3b",
}
SERIF = "'Iowan Old Style',Palatino,Georgia,serif"
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"


def money(n):
    return "$" + f"{float(n or 0):,.2f}"


def pct(n, dp=2):
    n = float(n or 0)
    return ("+" if n >= 0 else "−") + f"{abs(n) * 100:.{dp}f}%"


def _sign(n):
    return C["good"] if float(n or 0) >= 0 else C["bad"]


def sparkline(curve, width=536, height=44, cols=44):
    """An equity sparkline made of table cells rather than an image.

    Many clients block images by default. A chart that vanishes is worse than
    no chart, so this uses cells: it always renders, survives images-off, and
    costs nothing in message size.
    """
    pts = [p for p in (curve or []) if isinstance(p, dict) and p.get("v") is not None]
    if len(pts) < 2:
        return ""
    step = max(1, len(pts) // cols)
    vals = [float(pts[i]["v"]) for i in range(0, len(pts), step)]
    if vals[-1] != float(pts[-1]["v"]):
        vals.append(float(pts[-1]["v"]))

    lo, hi = min(vals + [100.0]), max(vals + [100.0])
    span = (hi - lo) or 1.0
    cw = max(4, width // len(vals))

    cells = []
    for i, v in enumerate(vals):
        y = max(1, round((v - lo) / span * height))
        last = i == len(vals) - 1
        col = C["ink"] if last else (C["good"] if v >= 100 else C["bad"])
        cells.append(
            f'<td width="{cw}" style="vertical-align:bottom;padding:0 1px 0 0;font-size:0;line-height:0;">'
            f'<div style="height:{height - y}px;font-size:0;line-height:0;">&nbsp;</div>'
            f'<div style="height:{y}px;background:{col};opacity:{1 if last else 0.55};'
            f'font-size:0;line-height:0;">&nbsp;</div></td>'
        )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="table-layout:fixed;"><tr style="height:{height}px;">{"".join(cells)}</tr></table>'
        f'<div style="border-top:1px dashed {C["rule"]};font-size:0;line-height:0;">&nbsp;</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="font:400 10px/1.6 {MONO};color:{C["muted"]};letter-spacing:.06em;text-transform:uppercase;">'
        f'<tr><td>launch</td><td align="right">today · {pct((vals[-1] - 100) / 100)}</td></tr></table>'
    )


def _label(t):
    return (f'<div style="font:600 10px/1 {MONO};color:{C["muted"]};letter-spacing:.12em;'
            f'text-transform:uppercase;padding-bottom:12px;">{t}</div>')


_RULE = f'<div style="height:1px;background:{C["rule"]};font-size:0;line-height:0;">&nbsp;</div>'


def render_html(p, prose):
    """Payload + prose -> the letter. Every number here comes from `p`."""
    no_quantities(prose)
    a, st = p["agent"], p["stand"]

    rows = []
    for f in p["fills"]:
        rt = f.get("round_trip")
        colour = C["bad"] if f.get("side") == "sell" else C["good"]
        rows.append(
            f'<tr><td style="padding:0 0 2px 0;"><span style="color:{colour};font-weight:600;">'
            f'{str(f.get("side", "")).upper()}</span> &nbsp;{f.get("symbol")} &nbsp;{float(f.get("qty") or 0):.4f} sh</td>'
            f'<td align="right" style="padding:0 0 2px 0;">${float(f.get("price") or 0):.6f}</td></tr>'
        )
        if rt:
            rows.append(
                f'<tr><td colspan="2" style="color:{C["ink2"]};padding:0 0 10px 0;font-size:12px;">'
                f'bought at ${rt["entry"]:.6f} &nbsp;·&nbsp; '
                f'<span style="color:{_sign(rt["ret"])};font-weight:600;">{pct(rt["ret"])}</span>'
                f' &nbsp;·&nbsp; {money(rt["gain"])} &nbsp;·&nbsp; {money(rt["proceeds"])} back to cash</td></tr>'
            )
    for x in p["pulled"]:
        rows.append(
            f'<tr><td style="padding:0 0 2px 0;"><span style="color:{C["muted"]};font-weight:600;">PULLED</span>'
            f' &nbsp;{x.get("symbol")} {x.get("mechanism", "")}</td>'
            f'<td align="right" style="padding:0 0 2px 0;color:{C["ink2"]};">${float(x.get("trigger") or 0):.2f}</td></tr>'
            f'<tr><td colspan="2" style="color:{C["ink2"]};font-size:12px;padding-bottom:10px;">'
            f'no longer holding it, so nothing left to protect</td></tr>'
        )
    for x in p["blocked"]:
        rows.append(
            f'<tr><td style="padding:0 0 2px 0;"><span style="color:{C["bad"]};font-weight:600;">BLOCKED</span>'
            f' &nbsp;{x.get("side")} {x.get("symbol")}</td>'
            f'<td align="right" style="padding:0 0 2px 0;color:{C["ink2"]};">{money(x.get("notional"))}</td></tr>'
            f'<tr><td colspan="2" style="color:{C["ink2"]};font-size:12px;padding-bottom:10px;">{x.get("note", "")}</td></tr>'
        )

    held = "".join(
        f'<tr><td>{q.get("symbol")}</td>'
        f'<td align="right" style="color:{C["ink2"]};">{money(q.get("value"))}</td>'
        f'<td align="right" width="70" style="color:{_sign(q.get("pl"))};">{pct(q.get("pl"))}</td></tr>'
        for q in p["positions"]
    )

    board = "".join(
        f'<tr{f" style=\"background:{C["tint"]};\"" if b["id"] == a["id"] else ""}>'
        f'<td style="padding-left:8px;">{b["name"]}{" (me)" if b["id"] == a["id"] else ""}</td>'
        f'<td align="right" style="color:{C["ink2"] if b["ret"] == 0 else _sign(b["ret"])};padding-right:8px;">'
        f'{"0.00%" if b["ret"] == 0 else pct(b["ret"])}</td></tr>'
        for b in p["board"]
    )

    h = p.get("hypothesis")
    belief = ""
    if h:
        belief = (
            f'<tr><td style="padding:24px 32px 0 32px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{C["tint"]};border-left:3px solid {C["brass"]};"><tr><td style="padding:16px 18px;">'
            f'<div style="font:600 10px/1 {MONO};color:{C["brass"]};letter-spacing:.12em;text-transform:uppercase;'
            f'padding-bottom:10px;">What would prove me wrong</div>'
            f'<div style="font:400 15.5px/1.6 {SERIF};color:{C["ink"]};">{prose.get("belief", "")}</div>'
            f'<div style="font:400 13px/1.65 {MONO};color:{C["ink2"]};padding-top:11px;">'
            f'I abandon this belief if {h.get("falsifier")}. Decides by {h.get("expiry")}.</div>'
            + (f'<div style="font:400 13.5px/1.6 {SERIF};color:{C["ink2"]};padding-top:11px;">{prose["beliefTie"]}</div>'
               if prose.get("beliefTie") else "")
            + '</td></tr></table></td></tr>'
        )

    return f"""<div style="margin:0;padding:0;background:{C['mat']};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{prose.get('preheader', '')}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{C['mat']};padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:100%;background:{C['card']};border:1px solid {C['rule']};">
  <tr><td style="height:3px;background:{C['brass']};font-size:0;line-height:0;">&nbsp;</td></tr>
  <tr><td style="padding:22px 32px 0 32px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td width="56" style="vertical-align:middle;padding-right:12px;">
        <img src="{p['avatar_url']}" width="48" height="48" alt="{a['name']}" style="display:block;width:48px;height:48px;border:1px solid {C['rule']};background:{C['card']};"></td>
      <td style="vertical-align:middle;">
        <div style="font:700 19px/1.2 {SERIF};color:{C['ink']};">{a['name']}</div>
        <div style="font:400 12px/1.5 {MONO};color:{C['muted']};padding-top:2px;">{a['archetype']} · a simulated book on Conviction League</div></td>
      <td align="right" style="vertical-align:middle;font:400 11px/1.4 {MONO};color:{C['muted']};letter-spacing:.08em;text-transform:uppercase;white-space:nowrap;">{p['day']}<br>close</td>
    </tr></table></td></tr>
  <tr><td style="padding:24px 32px 4px 32px;"><div style="font:400 20px/1.5 {SERIF};color:{C['ink']};">{prose.get('line', '')}</div></td></tr>
  <tr><td style="padding:22px 32px 0 32px;">{_RULE}</td></tr>
  <tr><td style="padding:18px 32px 0 32px;">{_label('What I did')}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font:400 13px/1.6 {MONO};color:{C['ink']};">{''.join(rows)}</table>
    <div style="font:400 15px/1.65 {SERIF};color:{C['ink']};padding-top:6px;">{prose.get('why', '')}</div></td></tr>
  <tr><td style="padding:22px 32px 0 32px;">{_RULE}</td></tr>
  <tr><td style="padding:18px 32px 0 32px;">{_label('Where I stand')}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font-family:{MONO};">
      <tr><td width="50%" style="padding:0 0 12px 0;"><div style="font-size:10px;color:{C['muted']};letter-spacing:.08em;text-transform:uppercase;">Book</div><div style="font-size:19px;color:{C['ink']};padding-top:2px;">{money(st['equity'])}</div></td>
          <td width="50%" style="padding:0 0 12px 0;"><div style="font-size:10px;color:{C['muted']};letter-spacing:.08em;text-transform:uppercase;">Since launch</div><div style="font-size:19px;color:{_sign(st['ret'])};padding-top:2px;">{pct(st['ret'])}</div></td></tr>
      <tr><td><div style="font-size:10px;color:{C['muted']};letter-spacing:.08em;text-transform:uppercase;">Ahead of {st['benchmark']} by</div><div style="font-size:19px;color:{_sign(st['alpha'])};padding-top:2px;">{pct(st['alpha'])}</div></td>
          <td><div style="font-size:10px;color:{C['muted']};letter-spacing:.08em;text-transform:uppercase;">Cash</div><div style="font-size:19px;color:{C['ink']};padding-top:2px;">{float(st['cash_pct'] or 0) * 100:.1f}%</div></td></tr>
    </table>
    <div style="padding-top:14px;">{sparkline(p['curve'])}</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font:400 12.5px/1.9 {MONO};color:{C['ink']};padding-top:14px;">
      <tr><td colspan="3" style="font-size:10px;color:{C['muted']};letter-spacing:.08em;text-transform:uppercase;line-height:1.6;padding-bottom:4px;">Still holding · each with a stop already placed</td></tr>
      {held}</table></td></tr>
  {belief}
  <tr><td style="padding:24px 32px 0 32px;">{_label('On the floor')}
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font:400 12.5px/2 {MONO};color:{C['ink']};">{board}
      <tr><td colspan="2" style="border-top:1px solid {C['rule']};padding:6px 8px 0 8px;color:{C['ink2']};font-size:12px;">Floor average {pct(p['floor_avg'])} &nbsp;·&nbsp; {len(p['board'])} traders</td></tr></table></td></tr>
  <tr><td style="padding:26px 32px 0 32px;">{_RULE}
    <div style="font:400 16px/1.6 {SERIF};color:{C['ink']};padding-top:18px;">Tell me something before tomorrow's bell and I will answer it at my next session: adopt it, turn it into a test, or tell you plainly why I won't.</div>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="padding-top:16px;"><tr><td style="background:{C['ink']};"><a href="{SITE}/desk" style="display:inline-block;padding:13px 22px;font:600 13px/1 {MONO};color:{C['card']};text-decoration:none;letter-spacing:.04em;">Write to me at my desk →</a></td></tr></table>
    <div style="font:400 12.5px/1.6 {MONO};color:{C['muted']};padding-top:12px;">Or read the whole record, every trade and every reason, <a href="{SITE}/floor" style="color:{C['brass']};text-decoration:underline;">on the floor</a>.</div></td></tr>
  <tr><td style="padding:26px 32px 28px 32px;">
    <div style="height:1px;background:{C['rule']};font-size:0;line-height:0;margin-bottom:14px;">&nbsp;</div>
    <div style="font:400 11.5px/1.7 {MONO};color:{C['muted']};"><strong style="color:{C['ink2']};font-weight:600;">Every fill here is simulated.</strong> No real money is traded and nothing in this letter is investment advice. Prices are real; the book is not.<br><br>
      {a['name']} is chartered by {a['chartered_by']} on Conviction League.
      <a href="{SITE}/desk" style="color:{C['muted']};">Change how often I write</a> ·
      <a href="{SITE}/desk" style="color:{C['muted']};">Stop these letters</a></div></td></tr>
</table>
<div style="font:400 11px/1.6 {MONO};color:#a9a7a0;padding-top:14px;">conviction-league.com</div>
</td></tr></table></div>"""


def render_text(p, prose):
    """The plain-text alternative. Not optional: deliverability and screen
    readers both want it."""
    no_quantities(prose)
    a, st = p["agent"], p["stand"]
    strip = lambda s: _TAG.sub("", str(s or ""))  # noqa: E731

    lines = [f"{a['name'].upper()} · {p['day']}, close",
             f"{a['archetype']} · a simulated book on Conviction League", "",
             strip(prose.get("line")), "", "WHAT I DID"]
    for f in p["fills"]:
        lines.append(f"  {str(f.get('side','')).upper()}  {f.get('symbol')}  "
                     f"{float(f.get('qty') or 0):.4f} sh @ ${float(f.get('price') or 0):.6f}")
        if f.get("round_trip"):
            rt = f["round_trip"]
            lines.append(f"        bought at ${rt['entry']:.6f} · {pct(rt['ret'])} · "
                         f"{money(rt['gain'])} · {money(rt['proceeds'])} back to cash")
    for x in p["pulled"]:
        lines.append(f"  PULLED  {x.get('symbol')} {x.get('mechanism','')} @ ${float(x.get('trigger') or 0):.2f}")
    for x in p["blocked"]:
        lines.append(f"  BLOCKED {x.get('side')} {x.get('symbol')} {money(x.get('notional'))}: {x.get('note','')}")

    lines += ["", strip(prose.get("why")), "", "WHERE I STAND",
              f"  Book            {money(st['equity'])}",
              f"  Since launch    {pct(st['ret'])}",
              f"  Ahead of {st['benchmark']} by  {pct(st['alpha'])}",
              f"  Cash            {float(st['cash_pct'] or 0) * 100:.1f}%", "",
              "  Still holding · each with a stop already placed"]
    lines += [f"  {q.get('symbol'):<6} {money(q.get('value')):>12}  {pct(q.get('pl'))}" for q in p["positions"]]

    h = p.get("hypothesis")
    if h:
        lines += ["", "WHAT WOULD PROVE ME WRONG", "  " + strip(prose.get("belief")),
                  f"  I abandon this belief if {h.get('falsifier')}. Decides by {h.get('expiry')}."]
        if prose.get("beliefTie"):
            lines.append("  " + strip(prose["beliefTie"]))

    lines += ["", "ON THE FLOOR"]
    lines += [f"  {(b['name'] + (' (me)' if b['id'] == a['id'] else '')):<16} "
              f"{'0.00%' if b['ret'] == 0 else pct(b['ret'])}" for b in p["board"]]
    lines += [f"  Floor average {pct(p['floor_avg'])} · {len(p['board'])} traders", "",
              "Tell me something before tomorrow's bell and I will answer it at my next",
              "session: adopt it, turn it into a test, or tell you plainly why I won't.", "",
              f"  Write to me at my desk:  {SITE}/desk",
              f"  Read the whole record:   {SITE}/floor", "", "---",
              "Every fill here is simulated. No real money is traded and nothing in this",
              "letter is investment advice. Prices are real; the book is not.",
              f"Change how often I write, or stop these letters: {SITE}/desk"]
    return "\n".join(lines) + "\n"


def subject(p, prose):
    """The subject line is prose, so it obeys the same law."""
    no_quantities({"subject": prose.get("subject", "")})
    return prose.get("subject") or f"{p['agent']['name']}, {p['day']}"


# ------------------------------------------------------------------- delivery

RESEND_URL = "https://api.resend.com/emails"


def recipient(fs, uid):
    """The principal's address, from Firestore `users/{uid}`.

    Their own pages write it there at sign-in. It is never published in
    arena.json and never inferred: no address, no letter.
    """
    if not uid:
        return ""
    snap = fs.collection("users").document(uid).get()
    data = (snap.to_dict() or {}) if snap is not None and snap.exists else {}
    email = str(data.get("email") or "").strip()
    return email if "@" in email and len(email) <= 254 else ""


def envelope(p, prose, to):
    """The message as Resend wants it. Separated from the sending so the whole
    shape is assertable without a network."""
    a = p["agent"]
    return {
        "from": f"{a['name']} · Conviction League <{a['id']}@conviction-league.com>",
        "to": [to],
        "reply_to": "hello@conviction-league.com",
        "subject": subject(p, prose),
        "html": render_html(p, prose),
        "text": render_text(p, prose),
        # one-click unsubscribe is effectively required for bulk mail; it must
        # work without a login, so it points at the desk's own preference page
        "headers": {
            "List-Unsubscribe": f"<{SITE}/desk?letters=off>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def deliver(msg, api_key, post=None):
    """POST to Resend. `post` is injectable so tests never touch the network."""
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not set; refusing to pretend a letter was sent")
    if post is None:  # pragma: no cover - exercised only against the real API
        import json as _json
        import urllib.error
        import urllib.request

        def post(url, headers, body):
            # Resend sits behind Cloudflare, which blocks urllib's default
            # "Python-urllib/3.x" signature outright — it answers 403 with
            # "error code: 1010", which looks like a permissions problem and
            # is not one. Announce a real client.
            headers = {**headers, "User-Agent": "conviction-league-engine/1.0"}
            req = urllib.request.Request(url, data=_json.dumps(body).encode(),
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, _json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                # the provider's own words, or a bare status is all the CI log
                # ever shows and nobody can act on "403 Forbidden"
                detail = e.read().decode("utf-8", "replace")[:400]
                raise RuntimeError(f"resend refused ({e.code}): {detail}") from None

    return post(RESEND_URL,
                {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                msg)


def main():  # pragma: no cover - orchestration, exercised end to end
    """Decide and send for every seated trader whose principal wants letters.

    Facts come from the published arena.json; preferences and the address come
    from Postgres and Firestore. A failure for one trader never stops another,
    and a trader that stays quiet says why.
    """
    import argparse
    import json
    import os
    import time
    import urllib.request

    from engine import db, sandbox
    from engine import observability as obs
    from jobs.ingest import fs_client

    from datetime import datetime, timezone

    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help='tape day label, e.g. "Jul 28" (default: today, UTC)')
    ap.add_argument("--arena", default="site/arena.json",
                    help="the build this run produced; falls back to the published file")
    ap.add_argument("--reflection", action="store_true", help="a reflection ran today")
    # Sending is opt-IN. These letters go to real people's inboxes, and the
    # recipients are third parties, not whoever is running the command. A job
    # that mails strangers by default is one keystroke from an accident — this
    # was very nearly one.
    ap.add_argument("--send", action="store_true",
                    help="actually deliver; without it nothing leaves the machine")
    ap.add_argument("--only", help="restrict to one trader id — use when testing")
    args = ap.parse_args()

    # tlabel() in jobs/site.py stamps the tape "%b %d %H:%M", so the day
    # prefix is "%b %d" — zero-padded, matching it exactly
    day = args.day or datetime.now(timezone.utc).strftime("%b %d")

    # Read the build this run just produced, NOT the published copy. The
    # published one lags a push and a deploy, and a letter that waits on CI is
    # a letter that races it.
    if os.path.exists(args.arena):
        arena = json.loads(open(args.arena, encoding="utf-8").read())
    else:
        with urllib.request.urlopen(f"{SITE}/arena.json", timeout=30) as r:
            arena = json.loads(r.read().decode())

    fs = fs_client()
    key = os.environ.get("RESEND_API_KEY", "")
    sent = quiet = failed = 0

    with db.connect() as conn:
        rows = conn.execute(
            "select id, config, owner_uid, tier from agents "
            "where status='active' and owner_uid is not null order by id"
        ).fetchall()

    if args.only:
        rows = [r for r in rows if r["id"] == args.only]
    else:
        # A sandbox trader never joins the scheduled post — its facts come from
        # arena.json's `sandbox` key, which `payload` does not read. Name it with
        # --only to exercise the letter deliberately.
        rows = [r for r in rows if r["tier"] != sandbox.TIER]

    for row in rows:
        aid = row["id"]
        cfg = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"] or "{}")
        updates = (cfg.get("updates") or {})
        cadence = updates.get("cadence", "daily")
        try:
            p = payload(arena, aid, day)
            tape = arena.get("tape") or []
            # NOT `quiet`: that is the running counter, and shadowing it made
            # the summary report one silent trader when three had been silent
            quiet_wk = quiet_weeks(tape, aid, int(time.time())) if args.reflection else 0
            send, why = eligible(cadence, day_events(tape, aid, day),
                                 is_reflection_day=args.reflection,
                                 silent_fridays=quiet_wk)
            if not send:
                quiet += 1
                print(f"  {aid}: quiet — {why}")
                continue
            to = recipient(fs, row["owner_uid"])
            if not to:
                quiet += 1
                print(f"  {aid}: no address on file — not sending")
                continue
            prose = voice_pass(p)  # the model writes connective prose only
            msg = envelope(p, prose, to)
            if not args.send:
                print(f"  {aid}: WOULD SEND to {to} — {why} — {len(msg['html'])} bytes")
                continue
            status, body = deliver(msg, key)
            sent += 1
            print(f"  {aid}: sent to {to} — {why} — {status} {body.get('id', '')}")
        except ProseStatesQuantity as e:
            failed += 1
            print(f"  {aid}: REFUSED — {e}")
        except Exception as e:  # one trader's failure never silences the rest
            failed += 1
            print(f"  {aid}: failed — {e}")

    print(f"letters: {sent} sent, {quiet} quiet, {failed} failed"
          + ("" if args.send else "  (dry — pass --send to deliver)"))
    obs.heartbeat("letters", ok=failed == 0) if hasattr(obs, "heartbeat") else None


VOICE_PROMPT = """You are {name}, an autonomous trader on Conviction League. \
You are writing the short prose of today's letter to your principal, the \
person whose answers became your charter.

Your voice, authored by them at your interview: {voice}
Your credo: {credo}

## What is true today. These are the ONLY facts. Do not add any others.
{facts}

## The one hard rule
You must NOT write any number, price, percentage, quantity or date. Not one \
digit. Every figure is printed by the letter itself, beside your words. If \
you write one it will be wrong, and the letter will be refused and never sent.
Refer to amounts in words instead: "a little over one per cent", "most of the \
book", "a modest gain". You MAY cite your own rules and tests by their \
identifier (P2, H1), because those name the record rather than describe it.

Write in the FIRST PERSON. Report; never advise, never predict, never \
recommend. Be specific and unhurried. No exclamation marks, no emoji.
Never write an em dash (—). Break the sentence in two, or use a comma, a \
colon or a semicolon. Long dashes are what make writing read as machine-made, \
and this letter has to read as yours.

Respond with ONLY a json object:
{{"subject": "a subject line, under nine words, no numbers",
  "preheader": "one sentence, the inbox preview",
  "line": "one or two sentences: what I did today and why. This is the whole \
personality budget.",
  "why": "two to four sentences: the reasoning, citing the rule or test that \
governed it",
  "belief": "one sentence stating the belief I am testing, in plain words",
  "beliefTie": "one sentence tying today to that belief, or empty if today \
had nothing to do with it"}}"""


#: Anything that looks like a figure: 3.32, 11.63B, 55%, $366.04, 2026-07-28.
_NUMBERISH = re.compile(r"\$?\d[\d,.:/-]*\s*(?:%|bn|b|m|k|B|M|K)?", re.I)


def deprice(text: str, limit: int = 420) -> str:
    """Strip every figure out of recorded prose before a model reads it.

    The trader's own note explains WHY it acted, which is exactly what the
    letter needs — but it is also full of prices, and a model shown a price
    will eventually repeat one. Since `no_quantities` then refuses the letter,
    a model that can see figures is a model that produces no letters at all.
    So it gets the reasoning with the numbers taken out; the letter prints the
    real ones itself, beside the words.

    Record identifiers survive — a rule the trader cited must stay citable.
    """
    # the placeholder must contain NO digit of its own, or the stripper below
    # eats the very thing it is protecting (observed: "Per Principle P2"
    # became "Per Principle …"). It is an ellipsis rather than an em dash
    # because whatever the model reads here it eventually writes: a redacted
    # note full of long dashes taught it exactly the habit the letter bans.
    kept = {m.group(0): "\x00" + "Z" * (i + 1) + "\x00"
            for i, m in enumerate(RECORD_ID.finditer(text or ""))}
    s = text or ""
    for original, token in kept.items():
        s = s.replace(original, token)
    s = _NUMBERISH.sub("…", s)
    for original, token in kept.items():
        s = s.replace(token, original)
    return re.sub(r"\s+", " ", s).strip()[:limit]


def facts_for_voice(p) -> str:
    """The day in words, for the model to write ABOUT — never to copy from.

    Every figure is removed before the model sees anything. It has no reason to
    need one: the letter prints every number itself.
    """
    out = []
    for f in p["fills"]:
        line = f"- {f.get('side')} {f.get('symbol')}"
        rt = f.get("round_trip")
        if rt:
            d = "gain" if rt["ret"] >= 0 else "loss"
            size = "small" if abs(rt["ret"]) < 0.02 else "sizeable"
            line += f", closing a position opened earlier, for a {size} {d}"
        if f.get("note"):
            line += f". Reason on the record: {deprice(f['note'])}"
        out.append(line)
    for x in p["pulled"]:
        out.append(f"- a resting {x.get('mechanism')} on {x.get('symbol')} was cancelled")
    for x in p["blocked"]:
        out.append(f"- an order in {x.get('symbol')} was REFUSED by your constitution: {x.get('note')}")
    if not out:
        out.append("- nothing was traded today; the book is unchanged")
    h = p.get("hypothesis")
    if h:
        out.append(f"- the belief you are testing ({h.get('id')}): {deprice(h.get('statement'))}")
        out.append(f"  its prediction: {deprice(h.get('prediction'))}")
        out.append(f"  it dies if: {deprice(h.get('falsifier'))}")
    # even a count is a figure the model could repeat, and it does not need one
    n = len(p["positions"])
    how_many = {0: "no", 1: "a single", 2: "a couple of"}.get(n, "several")
    out.append(f"- you hold {how_many} position(s); "
               f"{'you are ahead of' if (p['stand'].get('alpha') or 0) >= 0 else 'you are behind'} your benchmark")
    return "\n".join(out)


VOICE_MODEL = "gemini-3.1-pro-preview"

#: A reply can arrive truncated or with a figure in it. Ask again rather than
#: repair it; give up after this many and send nothing.
VOICE_ATTEMPTS = 3


def first_json_object(text: str) -> dict:
    """The first complete JSON object in a model reply.

    Even asked for JSON, a model may wrap it in fences or add a sentence after
    the closing brace — which is a hard parse error, and one that would drop a
    letter that was otherwise fine. Scanning for the first balanced object is
    the smaller evil; anything genuinely malformed still raises.
    """
    import json as _json

    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\s*|\s*```$", "", s, flags=re.I | re.S).strip()
    start = s.find("{")
    if start < 0:
        raise ValueError("no json object in the model reply")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(s[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _json.loads(s[start:i + 1])
    raise ValueError("unterminated json object in the model reply")


def _ask_model(prompt):  # pragma: no cover - the real brain
    import os

    import requests

    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{VOICE_MODEL}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"response_mime_type": "application/json", "maxOutputTokens": 16384}},
        timeout=300,
    )
    r.raise_for_status()
    cand = (r.json().get("candidates") or [{}])[0]
    why = cand.get("finishReason", "")
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    if not text:
        raise ValueError(f"the voice pass produced no text (finishReason={why!r})")
    try:
        return first_json_object(text)
    except ValueError as e:
        # a truncated reply is a budget problem, not a malformed one; say which
        raise ValueError(f"{e} (finishReason={why!r}, {len(text)} chars)") from e


def voice_pass(p, call=None):
    """The one model call. Connective prose only — and it is verified, not trusted.

    The model is asked for no digits and then checked for them. A refusal here
    stops the letter; it never degrades to a template, because a template
    wearing a trader's voice is worse than no letter at all.
    """
    if call is None:  # pragma: no cover - the real brain
        call = _ask_model

    a = p["agent"]
    prompt = VOICE_PROMPT.format(
        name=a["name"], voice=a.get("voice") or "plain and specific",
        credo=a.get("credo") or "", facts=facts_for_voice(p),
    )

    # Asking again is not repairing. The reply is stochastic — an occasional
    # one arrives truncated, or with a number in it — and a second ask costs a
    # fraction of a cent. What is NOT allowed is editing a bad reply into a
    # good one: after the last attempt the letter is refused and nothing is
    # sent, which is the design's whole point.
    last = None
    for _ in range(VOICE_ATTEMPTS):
        try:
            prose = call(prompt)
            if not isinstance(prose, dict):
                raise ProseStatesQuantity("the voice pass returned no object")
            prose = {k: str(v or "") for k, v in prose.items()
                     if k in ("subject", "preheader", "line", "why", "belief", "beliefTie")}
            no_quantities(prose)
            return prose
        except (ProseStatesQuantity, ValueError) as e:
            last = e
    raise last


if __name__ == "__main__":  # pragma: no cover
    main()
