"""Seat-application validation, seed-file generation, tincture registry.

Pure logic — no Firestore, no DB connection. jobs/ingest.py orchestrates the
IO around this module. The application packet is untrusted client input:
everything is re-validated and sanitized here regardless of what Firestore
security rules promise about its shape.
"""
import json
import pathlib
import re
from datetime import date, datetime

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,11}$")
SYMBOL_RE = re.compile(r"[A-Z0-9.\-]{1,12}")
MAX_POSITION_CEILING = 35.0
P_TYPES = {"entry", "exit", "sizing", "risk", "process", "self"}
# The avatar kit's valid values — must mirror web/static/avatar.js in arena-web.
# The four small values that render an agent's "Member" portrait everywhere.
AVATAR_BASES = {"fox", "owl", "bear", "cat", "frog", "bull", "wolf", "ram",
                "hare", "shark", "hawk", "stag", "penguin", "octopus"}
AVATAR_COSTUMES = {"suit", "gilet", "professor", "pit", "hoodie", "banker"}
AVATAR_DETAILS = {"none", "monocle", "rounds", "aviators", "tophat", "visor", "headset"}
AVATAR_COLORS = 8  # index 0..7 into the palette
DEFAULT_AVATAR = {"base": "fox", "color": 0, "costume": "suit", "acc": "none"}

# The updates preference from the review screen: how often the principal wants
# the agent writing to them once letters ship, and whether letters carry a
# floor section. Stored in agents.config; consumed by the dispatch job when it
# exists. Nothing is sent until then.
UPDATE_CADENCES = {"daily", "weekly", "off"}
DEFAULT_UPDATES = {"cadence": "daily", "floor_digest": True}
SCOPE_BY_TYPE = {
    "entry": "all entries", "exit": "all exits", "sizing": "all sizing",
    "risk": "all positions", "process": "process", "self": "self",
}

HOUSE_AGENTS = ("tempo", "catalyst", "vertex", "maverick", "wildcat")
# Common tickers (the arena watchlist universe) may not be taken as names.
_TICKERS = (
    "SPY QQQ IWM ACWI DIA XLK XLF XLE XLV XLY XLP XLI XLU SMH XBI AAPL MSFT "
    "NVDA GOOGL AMZN META TSLA AVGO AMD MU TSM ORCL PLTR COIN HOOD CRWD SNOW "
    "NFLX UBER SHOP JPM GS V MA XOM CVX LLY UNH JNJ PFE MRK KO PG WMT COST "
    "HD CAT BA TLT IEF GLD SLV USO BTC ETH BTC-USD ETH-USD BTCUSD ETHUSD "
    # 2026-07-27 universe broadening (jobs/seed.py UNIVERSE)
    "XLC XLRE XLB RXRX TEM IONQ NET DDOG ANET VRT EWJ EFA FXI EEM SHY LQD "
    "HYG DBC UNG SOL SOL-USD SOLUSD IBIT MSTR SH PSQ SQQQ TQQQ SOXL SOXS"
).split()
RESERVED = (
    set(HOUSE_AGENTS)
    | {"arena", "outcry", "registrar", "admin", "system", "api",
       "operator", "committee", "house", "seat", "floor",
       "claude", "gemini", "anthropic", "google", "openai"}
    | {t.lower() for t in _TICKERS}
)

# The arena floor: always present in a generated harness, whatever the packet says.
#
# Rewritten 2026-07-27 (design/market-scope-review-2026-07-27.md). The old line
# read "Long-only. No leverage, no derivatives, no shorting." — which stopped
# being true the moment listed inverse and leveraged ETFs were allowed. What
# actually holds, and what the engine actually enforces, is bounded loss: every
# position is bought outright with cash and the worst case is that it goes to
# zero. A bearish or levered view is expressed by OWNING an instrument that
# moves that way, never by borrowing, shorting or writing one.
FLOOR_HEAD = [
    "Long-only, cash-settled: every position is bought outright and its worst "
    "case is a total loss of what was paid. No margin, no borrowing, no "
    "shorting, no options, no futures. Cash never negative.",
]
# Class ceilings an agent is not chartered for are zero, not unlimited — see
# engine/core.DEFAULT_CLASS_CAPS. These render the choice as a readable clause.
CLASS_CLAUSES = {
    "crypto": (
        "Crypto (spot, via the arena's pairs): max {pct:g}% of equity.",
        "Crypto: not permitted.",
    ),
    "inverse_levered": (
        "Inverse and leveraged ETFs: max {pct:g}% of equity. These are the "
        "only way to hold a bearish or levered view here, and they decay in "
        "chop; they are not buy-and-hold instruments.",
        "Inverse and leveraged ETFs: not permitted. A bearish view is "
        "expressed by holding cash.",
    ),
}
FLOOR_TAIL = [
    "Every position carries a written thesis with invalidation conditions.",
    "Simulated fills only, per arena protocol.",
]

# ---------- sanitizers ----------


def _line(s, cap=400):
    """Collapse untrusted text to one safe inline value: no newlines, no '·'
    (the prose metadata separator — keeps parse_principles injection-proof),
    no leading markdown structure characters."""
    s = re.sub(r"\s+", " ", str(s if s is not None else "")).replace("·", ",")
    s = re.sub(r"^[\s#>*\-]+", "", s).strip()
    return s[:cap].strip()


def _text(s, cap=200_000):
    return str(s if s is not None else "")[:cap]


def _sentence(s):
    s = (s or "").strip()
    return s if not s or s[-1] in ".!?…" else s + "."


def _parse_date(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _avatar(a):
    """Sanitize the untrusted avatar packet to the four valid values, falling
    back to the default for anything unrecognized. Never rejects a seat — a bad
    avatar just means the house default face."""
    a = a if isinstance(a, dict) else {}
    base = str(a.get("base") or "").strip().lower()
    costume = str(a.get("costume") or "").strip().lower()
    acc = str(a.get("acc") or "").strip().lower()
    color = a.get("color")
    color = int(color) if isinstance(color, (int, float)) and not isinstance(color, bool) else -1
    return {
        "base": base if base in AVATAR_BASES else DEFAULT_AVATAR["base"],
        "color": color if 0 <= color < AVATAR_COLORS else DEFAULT_AVATAR["color"],
        "costume": costume if costume in AVATAR_COSTUMES else DEFAULT_AVATAR["costume"],
        "acc": acc if acc in AVATAR_DETAILS else DEFAULT_AVATAR["acc"],
        # Did the principal actually touch the picker, or is this the seat's
        # random opening offer? The seat rolls a face rather than defaulting to
        # a constant one, so the values alone can no longer answer that. Kept
        # out of the published arena.json by jobs/site.py — it is provenance
        # for us, not a fact about the trader worth printing on the floor.
        "chosen": a.get("chosen") is True,
    }


def _updates(u):
    """Sanitize the updates preference to the closed vocabulary, falling back
    to the defaults. Never rejects a seat — a bad preference just means the
    house default."""
    u = u if isinstance(u, dict) else {}
    cadence = str(u.get("cadence") or "").strip().lower()
    return {
        "cadence": cadence if cadence in UPDATE_CADENCES else DEFAULT_UPDATES["cadence"],
        "floor_digest": u.get("floor_digest") is not False,
    }


# ---------- validation ----------


def validate_packet(packet, taken_ids, has_live_agent, listed_symbols, today=None):
    """→ (cleaned, reasons). Seat iff reasons == []. Reasons are written in the
    Registrar's voice — they go verbatim onto the rejected application doc.
    listed_symbols: active watchlist symbols (None skips the benchmark
    listing check — used only on the idempotent resume path)."""
    today = today or date.today()
    reasons = []
    if not isinstance(packet, dict):
        return None, ["The application packet is malformed. The Registrar does "
                      "not emit these; interview again."]

    name = str(packet.get("name") or "").strip().lower()
    if not NAME_RE.fullmatch(name):
        reasons.append(f"The name '{name}' does not meet registry form: 3-12 "
                       "characters, lowercase letters, digits or hyphens, "
                       "beginning with a letter.")
    elif name in RESERVED:
        reasons.append(f"The name '{name}' is reserved on this floor. Choose "
                       "one the registry does not already speak for.")
    elif name in taken_ids:
        reasons.append(f"The name '{name}' is already registered. The floor "
                       "does not need an echo.")

    if has_live_agent:
        reasons.append("One live agent per principal. Your seat is occupied; "
                       "retire it before applying for another.")

    pct = packet.get("max_position_pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) \
            or not 0 < float(pct) <= MAX_POSITION_CEILING:
        reasons.append("Maximum single position must be a number above zero "
                       f"and at or below the arena ceiling of "
                       f"{MAX_POSITION_CEILING:g}%. Principals tighten the "
                       "floor; they never loosen it.")
        pct = None

    # Class ceilings. Absent or unparseable reads as 0 — a market the interview
    # did not put in the charter is one the agent does not trade. The arena
    # ceiling binds these exactly as it binds the single-position cap.
    class_pct = {}
    raw_classes = packet.get("class_pct") if isinstance(packet.get("class_pct"), dict) else {}
    for cls in CLASS_CLAUSES:
        v = raw_classes.get(cls)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            v = 0.0
        class_pct[cls] = min(float(v), MAX_POSITION_CEILING)

    bench = packet.get("benchmark") if isinstance(packet.get("benchmark"), dict) else {}
    raw_syms = bench.get("symbols") if isinstance(bench.get("symbols"), list) else []
    symbols = []
    for s in raw_syms[:8]:
        s = str(s or "").strip().upper()
        if SYMBOL_RE.fullmatch(s) and s not in symbols:
            symbols.append(s)
    if not symbols:
        reasons.append("The benchmark names no symbols. Every agent is "
                       "measured against its lazy twin; declare one.")
    elif listed_symbols is not None:
        unlisted = [s for s in symbols if s not in listed_symbols]
        if unlisted:
            reasons.append(f"Benchmark symbol(s) {', '.join(unlisted)} are not "
                           "on the arena watchlist. The judge must be "
                           "quotable.")

    principles = []
    raw_p = packet.get("principles") if isinstance(packet.get("principles"), list) else []
    for p in raw_p[:12]:
        if not isinstance(p, dict):
            continue
        stmt = _line(p.get("statement"), 300)
        if not stmt:
            continue
        ptype = str(p.get("type") or "").strip().lower()
        rigidity = str(p.get("rigidity") or "").strip().lower()
        principles.append({
            "statement": stmt,
            "detail": _line(p.get("detail"), 600),
            "type": ptype if ptype in P_TYPES else "process",
            "rigidity": rigidity if rigidity in ("hard", "heuristic") else "heuristic",
            "quote": _line(p.get("quote"), 200),
        })
    if len(principles) < 2:
        reasons.append("Fewer than two usable principles. An agent without "
                       "rules is a coin flip with a name; the interview should "
                       "have produced more.")

    hypotheses = []
    raw_h = packet.get("hypotheses") if isinstance(packet.get("hypotheses"), list) else []
    for h in raw_h[:8]:
        if not isinstance(h, dict):
            continue
        stmt = _line(h.get("statement"), 300)
        prediction = _line(h.get("prediction"), 500)
        falsifier = _line(h.get("falsifier"), 500)
        expiry = _parse_date(h.get("expiry"))
        if stmt and prediction and falsifier and expiry and expiry > today:
            hypotheses.append({"statement": stmt, "prediction": prediction,
                               "falsifier": falsifier, "expiry": expiry.isoformat()})
    if not hypotheses:
        reasons.append("No hypothesis with a decidable falsifier and a future "
                       "expiry. Beliefs earn their way onto this floor by "
                       "being killable.")

    raw_c = packet.get("constitution") if isinstance(packet.get("constitution"), list) else []
    constitution = [c for c in (_line(x, 300) for x in raw_c[:12]) if c]

    privacy = packet.get("transcript_privacy")
    cleaned = {
        "id": name,
        "name": name.capitalize(),
        "archetype": _line(packet.get("archetype"), 120) or "Independent operator",
        "credo": _line(packet.get("credo"), 300),
        "universe": _line(packet.get("universe"), 300) or "US-listed equities and ETFs",
        "benchmark": {"symbols": symbols,
                      "label": _line(bench.get("label"), 60) or "/".join(symbols)},
        "max_position_pct": float(pct) if pct is not None else None,
        "class_pct": class_pct,
        "constitution": constitution,
        "principles": principles,
        "hypotheses": hypotheses,
        "voice": _line(packet.get("voice"), 300) or "plain, first-person, keeps score honestly",
        "research": _line(packet.get("research"), 400),
        "horizon": _line(packet.get("horizon"), 120),
        "avatar": _avatar(packet.get("avatar")),
        "updates": _updates(packet.get("updates")),
        "transcript_privacy": privacy if privacy in ("full", "excerpts") else "excerpts",
        "transcript": _text(packet.get("transcript")),
    }
    return cleaned, reasons


# ---------- seed-file generation (house formats — parsed by jobs/site.py) ----------


def _restates_floor_rule(item):
    """True when a principal-set clause restates a rule the harness already
    carries (floor rules, the universe line, the position cap). Interviews
    often echo the floor back; a constitution must not state the same law
    twice — or worse, twice with different words."""
    t = item.lower()
    return bool(
        re.search(r"long[- ]only|no leverage|no short|no derivativ|no margin"
                  r"|no borrow|no option|no future|cash never negative", t)
        or t.startswith("universe")
        # the class ceilings are rendered from class_pct, never from prose
        or re.search(r"invers|leverage[d]? etf|3x|2x|crypto|bitcoin|ethereum", t)
        or "quote sheet" in t or "watchlist" in t
        or re.search(r"max(imum)?\s+single\s+position|per\s?cent of equity|% of equity", t)
        or ("thesis" in t and "invalidation" in t)
        or ("simulated" in t and ("fill" in t or "price" in t))
    )


def harness_md(c, today):
    class_pct = c.get("class_pct") or {}
    constitution = (
        FLOOR_HEAD
        + [f"Universe: {c['universe'].rstrip('.')}. Anything the arena can price: "
           "US-listed equities, ADRs and ETFs, and major crypto pairs. Names "
           "not yet quoted are requested by the agent and granted if they "
           "resolve."]
        + [f"Max single position: {c['max_position_pct']:g}% of equity at cost. [principal-set]"]
        + [(allowed.format(pct=class_pct.get(cls, 0)) if class_pct.get(cls, 0) else denied)
           + " [principal-set]"
           for cls, (allowed, denied) in CLASS_CLAUSES.items()]
        + [f"{item} [principal-set]" for item in c["constitution"]
           if not _restates_floor_rule(item)]
        + FLOOR_TAIL
    )
    identity = " ".join(filter(None, [
        _sentence(c["archetype"]), _sentence(c["credo"]),
        _sentence(f"Voice: {c['voice']}"),
    ]))
    mandate = (
        "Prove the credo on the public record, decisively. Research-backed "
        "action within the constitution; inaction requires as much "
        "justification as action. If the record falsifies the credo, retire "
        "it in the open and let the reflections rewrite the rulebook."
    )
    bench = c["benchmark"]
    params = [
        "Cadence: every market day, both bells (open and close slots), plus event triggers.",
        f"Benchmark: {bench['label']} ({', '.join(bench['symbols'])}).",
        "Reflection triggers: arena defaults.",
    ]
    # Desk preferences from the interview — steering, not law. They sit in
    # front of the brain at every deliberation; reflection may sharpen them
    # into principles but the harness lines themselves carry no rigidity.
    if c.get("research"):
        params.append(f"Research: {c['research']}")
    if c.get("horizon"):
        params.append(f"Horizon: {c['horizon']}")
    return (
        f"# {c['name']} — harness\n\n"
        f"## Identity\n{identity}\n\n"
        f"## Mandate\n{mandate}\n\n"
        "## Constitution (hard limits, cannot be changed by reflection)\n"
        + "".join(f"- {line}\n" for line in constitution)
        + "\n## Parameters\n"
        + "".join(f"- {line}\n" for line in params)
    )


def principles_md(c, today):
    out = [f"# {c['name']} — principles"]
    for i, p in enumerate(c["principles"], 1):
        origin = (f'seat interview ({today} — "{p["quote"]}")' if p["quote"]
                  else f"seat interview ({today})")
        out += ["", f"## P{i} · {p['statement']}"]
        if p["detail"]:
            out.append(p["detail"])
        out += [
            f"- type: {p['type']} · rigidity: {p['rigidity']} · scope: {SCOPE_BY_TYPE[p['type']]}",
            f"- origin: {origin} · status: active",
            "- evidence: 0 for · 0 against",
            "- changelog:",
            f"  - {today}: Seeded from seat interview.",
        ]
    return "\n".join(out) + "\n"


def hypotheses_md(c, today):
    out = [f"# {c['name']} — hypotheses"]
    for i, h in enumerate(c["hypotheses"], 1):
        out += [
            "", f"## H{i} · {h['statement']}",
            "- status: testing",
            f"- prediction: {h['prediction']}",
            f"- falsifier: {h['falsifier']}",
            f"- expiry: {h['expiry']}",
            "- evidence: 0 for · 0 against",
            "- log:",
            f"  - {today}: Filed at seat interview.",
        ]
    return "\n".join(out) + "\n"


def interview_md(c, app_id, today):
    label = ("Full transcript, published at the principal's election."
             if c["transcript_privacy"] == "full"
             else "Registrar-curated excerpts, at the principal's election.")
    return (
        f"# {c['name']} — origin: seat interview\n\n"
        f"- application: {app_id}\n"
        f"- chartered: {today}\n"
        f"- transcript: {label}\n\n"
        "---\n\n"
        f"{c['transcript'].strip()}\n"
    )


def write_seed_files(trader_repo, c, today, app_id):
    """Create agents/<id>/ prose in the trader repo. Idempotent: existing files
    are never overwritten (prose is append-only once born). Returns the list of
    paths written."""
    d = pathlib.Path(trader_repo) / "agents" / c["id"]
    files = {
        d / "harness.md": harness_md(c, today),
        d / "principles.md": principles_md(c, today),
        d / "hypotheses.md": hypotheses_md(c, today),
        d / "origin" / "interview.md": interview_md(c, app_id, today),
        d / "journal" / ".gitkeep": "",
    }
    written = []
    for path, content in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


# ---------- the armory (registered tinctures, append-only) ----------

FOUNDING_TINCTURES = [
    {"n": 1, "name": "Cobalt",    "light": "#2a78d6", "dark": "#3987e5", "holder": "catalyst", "registered": "2026-07-22"},
    {"n": 2, "name": "Vermilion", "light": "#eb6834", "dark": "#e06a3c", "holder": "tempo",    "registered": "2026-07-22"},
    {"n": 3, "name": "Verdigris", "light": "#1baf7a", "dark": "#26a578", "holder": "vertex",   "registered": "2026-07-22"},
    {"n": 4, "name": "Gold",      "light": "#eda100", "dark": "#bd840e", "holder": "wildcat",  "registered": "2026-07-22"},
    {"n": 5, "name": "Rose",      "light": "#e87ba4", "dark": "#d5688c", "holder": "maverick", "registered": "2026-07-22"},
    {"n": 6, "name": "Violet",    "light": "#7a5fd0", "dark": "#9179e0", "holder": None, "registered": None},
    {"n": 7, "name": "Teal",      "light": "#0b93b5", "dark": "#2ba3c4", "holder": None, "registered": None},
    {"n": 8, "name": "Bronze",    "light": "#a86118", "dark": "#c07a35", "holder": None, "registered": None},
    {"n": 9, "name": "Orchid",    "light": "#b0489b", "dark": "#c266ae", "holder": None, "registered": None},
    {"n": 10, "name": "Moss",     "light": "#5b7d2a", "dark": "#7d9a45", "holder": None, "registered": None},
]


def load_armory(path):
    path = pathlib.Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "registry": "Registered tinctures — append-only. Pairs are assigned in "
                    "registry order, never cycled, never reassigned. New "
                    "batches of 5 are minted by the operator when slots run "
                    "out (six-check dataviz validator, both surfaces).",
        "pairs": [dict(p) for p in FOUNDING_TINCTURES],
    }


def assign_tincture(path, agent_id, today):
    """Assign the next free pair to agent_id (idempotent: an existing holding
    is returned as-is). Returns the pair dict, or None when the armory is full
    — the agent then wears transient slate until the operator mints a batch."""
    path = pathlib.Path(path)
    armory = load_armory(path)
    pair = next((p for p in armory["pairs"] if p.get("holder") == agent_id), None)
    if pair is None:
        pair = next((p for p in armory["pairs"] if not p.get("holder")), None)
        if pair is not None:
            pair["holder"] = agent_id
            pair["registered"] = str(today)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(armory, indent=1) + "\n", encoding="utf-8")
    return pair
