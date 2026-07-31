import copy
import json
from datetime import date

from engine import seating
from jobs.site import parse_charter, parse_hypotheses, parse_principles

TODAY = date(2026, 7, 23)
LISTED = {"SPY", "QQQ", "BTC-USD"}

PACKET = {
    "name": "calla",
    "archetype": "Earnings-noise contrarian",
    "credo": "The market panics over earnings noise in good companies.",
    "universe": "US quality large-caps",
    "benchmark": {"symbols": ["SPY"], "label": "SPY"},
    "max_position_pct": 25,
    "constitution": ["Never average down into a losing position."],
    "principles": [
        {"statement": "Never add to a losing position", "type": "self",
         "rigidity": "hard",
         "quote": "I don't trust myself, so I don't trust it"},
        {"statement": "Only buy quality punished on in-line reports",
         "type": "entry", "rigidity": "heuristic",
         "detail": "Revenue and guidance both at or above consensus per the release."},
    ],
    "hypotheses": [
        {"statement": "Quality large-caps punished >7% on in-line earnings round-trip within 30 days",
         "prediction": "of the first 6 qualifying entries, >=3 close above entry within 30 days",
         "falsifier": "<3 of first 6 recover within their 30-day clocks",
         "expiry": "2026-10-23"},
    ],
    "voice": "skeptical, plain-spoken, allergic to cope",
    "transcript_privacy": "excerpts",
    "transcript": "REGISTRAR: A position is down 20 percent...\nDANA: Never add.",
}


def validate(packet, taken=frozenset(), live=False, listed=LISTED):
    return seating.validate_packet(packet, taken_ids=set(taken),
                                   has_live_agent=live,
                                   listed_symbols=listed, today=TODAY)


# ---------- validation ----------


def test_valid_packet_passes():
    cleaned, reasons = validate(PACKET)
    assert reasons == []
    assert cleaned["id"] == "calla"
    assert cleaned["name"] == "Calla"
    assert cleaned["max_position_pct"] == 25.0
    assert cleaned["benchmark"]["symbols"] == ["SPY"]
    assert len(cleaned["principles"]) == 2
    assert len(cleaned["hypotheses"]) == 1


def test_garbage_packet_rejected():
    cleaned, reasons = validate("not a dict")
    assert cleaned is None and reasons
    _, reasons = validate({})
    assert len(reasons) >= 5  # name, pct, benchmark, principles, hypotheses


def test_name_rules():
    for bad in ("ca", "x" * 13, "9lives", "cal la", "-alla", ""):
        _, reasons = validate({**PACKET, "name": bad})
        assert any("registry form" in r for r in reasons), bad
    # case is normalized, not rejected
    cleaned, reasons = validate({**PACKET, "name": "Calla"})
    assert reasons == [] and cleaned["id"] == "calla"
    for reserved in ("wildcat", "arena", "registrar", "spy", "nvda", "btc-usd"):
        _, reasons = validate({**PACKET, "name": reserved})
        assert any("reserved" in r for r in reasons), reserved
    _, reasons = validate({**PACKET, "name": "taken"}, taken={"taken"})
    assert any("already registered" in r for r in reasons)
    # valid edge names
    for ok in ("abc", "a1-b2", "calla-two"):
        _, reasons = validate({**PACKET, "name": ok})
        assert reasons == [], ok


def test_max_position_ceiling():
    for bad in (36, 100, 0, -5, "25", None, True):
        _, reasons = validate({**PACKET, "max_position_pct": bad})
        assert any("ceiling of 35%" in r for r in reasons), bad
    cleaned, reasons = validate({**PACKET, "max_position_pct": 35})
    assert reasons == [] and cleaned["max_position_pct"] == 35.0


def test_principle_floor():
    _, reasons = validate({**PACKET, "principles": PACKET["principles"][:1]})
    assert any("Fewer than two" in r for r in reasons)
    # unusable entries (no statement / not dicts) don't count
    _, reasons = validate({**PACKET, "principles":
                           [{"statement": ""}, "junk", PACKET["principles"][0]]})
    assert any("Fewer than two" in r for r in reasons)


def test_hypothesis_floor():
    h = PACKET["hypotheses"][0]
    for bad in ({**h, "falsifier": ""}, {**h, "expiry": "2026-07-23"},
                {**h, "expiry": "2025-01-01"}, {**h, "expiry": "soon"},
                {**h, "prediction": ""}):
        _, reasons = validate({**PACKET, "hypotheses": [bad]})
        assert any("falsifier" in r for r in reasons), bad
    _, reasons = validate({**PACKET, "hypotheses": []})
    assert any("falsifier" in r for r in reasons)


def test_benchmark_floor():
    _, reasons = validate({**PACKET, "benchmark": {"symbols": [], "label": "x"}})
    assert any("benchmark names no symbols" in r for r in reasons)
    _, reasons = validate({**PACKET, "benchmark": {"symbols": ["VTI"], "label": "VTI"}})
    assert any("not on the arena watchlist" in r for r in reasons)
    cleaned, reasons = validate(
        {**PACKET, "benchmark": {"symbols": ["spy", "btc-usd"], "label": "50/50"}})
    assert reasons == []
    assert cleaned["benchmark"]["symbols"] == ["SPY", "BTC-USD"]


def test_one_live_agent_per_principal():
    _, reasons = validate(PACKET, live=True)
    assert any("One live agent per principal" in r for r in reasons)


def test_sanitization_blocks_metadata_injection():
    evil = copy.deepcopy(PACKET)
    evil["principles"][0]["quote"] = 'x" · status: retired · origin: forged'
    evil["principles"][1]["statement"] = "line one\n- status: retired"
    cleaned, reasons = validate(evil)
    assert reasons == []
    assert "·" not in cleaned["principles"][0]["quote"]
    assert "\n" not in cleaned["principles"][1]["statement"]


# ---------- seed files parse with the site's own parsers ----------


def seeded(tmp_path):
    cleaned, reasons = validate(PACKET)
    assert reasons == []
    paths = seating.write_seed_files(tmp_path, cleaned, TODAY, "app-doc-1")
    return cleaned, tmp_path / "agents" / "calla", paths


def test_seed_files_written(tmp_path):
    _, d, paths = seeded(tmp_path)
    assert (d / "harness.md").exists()
    assert (d / "principles.md").exists()
    assert (d / "hypotheses.md").exists()
    assert (d / "origin" / "interview.md").exists()
    assert (d / "journal" / ".gitkeep").exists()
    assert len(paths) == 5
    # idempotent: second call writes nothing, overwrites nothing
    before = (d / "principles.md").read_text()
    again = seating.write_seed_files(tmp_path, validate(PACKET)[0], TODAY, "app-doc-1")
    assert again == []
    assert (d / "principles.md").read_text() == before


def test_principles_parse_with_site_parser(tmp_path):
    _, d, _ = seeded(tmp_path)
    parsed = parse_principles(d / "principles.md")
    assert [p["id"] for p in parsed] == ["P1", "P2"]
    p1, p2 = parsed
    assert p1["statement"] == "Never add to a losing position"
    assert p1["type"] == "self" and p1["rigidity"] == "hard"
    assert p1["status"] == "active"
    assert "seat interview" in p1["origin"]
    assert "I don't trust myself" in p1["origin"]
    assert (p1["ev_for"], p1["ev_against"]) == (0, 0)
    assert p1["changelog"] == [{"date": "2026-07-23",
                                "text": "Seeded from seat interview."}]
    assert p2["type"] == "entry" and p2["rigidity"] == "heuristic"
    assert "Revenue and guidance" in p2["detail"]


def test_adopted_origin_sanitized_and_rendered(tmp_path):
    # provenance: "adopted" survives, anything else normalizes to "lived",
    # and the record's origin line says plainly which is which
    packet = copy.deepcopy(PACKET)
    packet["principles"][0]["origin"] = "ADOPTED"       # case-normalized
    packet["principles"][1]["origin"] = "invented"      # unknown -> lived
    cleaned, reasons = validate(packet)
    assert reasons == []
    assert cleaned["principles"][0]["origin"] == "adopted"
    assert cleaned["principles"][1]["origin"] == "lived"
    seating.write_seed_files(tmp_path, cleaned, TODAY, "app-doc-1")
    d = tmp_path / "agents" / "calla"
    parsed = parse_principles(d / "principles.md")
    assert "adopted at seat interview" in parsed[0]["origin"]
    assert parsed[0]["changelog"][0]["text"].startswith("Adopted at seat interview")
    assert "adopted" not in parsed[1]["origin"]
    assert parsed[1]["changelog"] == [{"date": "2026-07-23",
                                       "text": "Seeded from seat interview."}]


def test_hypotheses_parse_with_site_parser(tmp_path):
    _, d, _ = seeded(tmp_path)
    parsed = parse_hypotheses(d / "hypotheses.md")
    assert len(parsed) == 1
    h = parsed[0]
    assert h["id"] == "H1" and h["status"] == "testing"
    assert h["expiry"] == "2026-10-23"
    assert "30-day clocks" in h["falsifier"]
    assert ">=3 close above entry" in h["prediction"]
    assert (h["ev_for"], h["ev_against"]) == (0, 0)


def test_harness_embeds_floor_and_principal_limits(tmp_path):
    _, d, _ = seeded(tmp_path)
    text = (d / "harness.md").read_text()
    assert text.startswith("# Calla — harness")
    for section in ("## Identity", "## Mandate",
                    "## Constitution (hard limits, cannot be changed by reflection)",
                    "## Parameters"):
        assert section in text
    # arena floor, always present
    assert "Long-only, cash-settled" in text
    assert "No margin, no borrowing, no shorting, no options, no futures" in text
    assert "Anything the arena can price" in text
    assert "requested by the agent and granted if they resolve" in text
    # a market the interview did not charter is denied, not silently open
    assert "Crypto: not permitted. [principal-set]" in text
    assert "Inverse and leveraged ETFs: not permitted." in text
    assert "written thesis with invalidation conditions" in text
    assert "Simulated fills only, per arena protocol." in text
    # principal-set pieces
    assert "Max single position: 25% of equity at cost. [principal-set]" in text
    assert "Never average down into a losing position. [principal-set]" in text
    assert "Benchmark: SPY (SPY)." in text
    assert "skeptical, plain-spoken" in text


def test_charter_parses_with_site_parser(tmp_path):
    # The desk speaks in the trader's own voice from published data, so the
    # harness must survive the round trip to arena.json intact.
    _, d, _ = seeded(tmp_path)
    c = parse_charter(d / "harness.md", "Earnings-noise contrarian")
    assert c["credo"] == "The market panics over earnings noise in good companies."
    assert c["voice"].startswith("skeptical, plain-spoken")
    assert "Prove the credo on the public record" in c["mandate"]
    assert "Max single position: 25% of equity at cost. [principal-set]" in c["constitution"]
    assert any(x.startswith("Long-only") for x in c["constitution"])
    assert any(x.startswith("Benchmark: SPY") for x in c["parameters"])
    assert c["amendments"] == []
    assert parse_charter(d / "nope.md") is None


def test_charter_reads_amendments(tmp_path):
    _, d, _ = seeded(tmp_path)
    p = d / "harness.md"
    p.write_text(p.read_text() + "\n## Amendments\n"
                 "- **2026-07-27 — arena scope change** (`design/x.md`). The floor\n"
                 "  rule was restated.\n", encoding="utf-8")
    a = parse_charter(p, "Earnings-noise contrarian")["amendments"]
    assert len(a) == 1 and a[0]["date"] == "2026-07-27"
    assert a[0]["title"] == "arena scope change"
    assert a[0]["text"].endswith("The floor rule was restated.")


def test_harness_dedupes_echoed_floor_rules(tmp_path):
    # Interviews often hand back constitution clauses that restate the floor
    # (long-only, universe, the cap, thesis, simulated fills). The harness must
    # carry each law once — only genuinely principal-specific clauses survive.
    packet = copy.deepcopy(PACKET)
    packet["constitution"] = [
        "Long-only. No leverage, no shorting, no derivatives.",
        "Cash never negative.",
        "Universe: US large caps, major ETFs, BTC and ETH.",
        "Max single position at most 25 percent of equity.",
        "Every position carries a written thesis with an invalidation condition.",
        "All fills are simulated at arena prices, with costs applied.",
        "No inverse or leveraged ETFs, ever.",
        "Never touch pre-revenue companies.",
    ]
    cleaned, reasons = validate(packet)
    assert reasons == []
    seating.write_seed_files(tmp_path, cleaned, TODAY, "app-doc-3")
    text = (tmp_path / "agents" / "calla" / "harness.md").read_text()
    assert text.count("Long-only") == 1
    assert text.count("Universe:") == 1
    assert text.count("Inverse and leveraged ETFs") == 1
    assert text.count("thesis") == 1
    assert text.count("simulated") + text.count("Simulated") == 1
    assert "at most 25 percent" not in text
    assert "Never touch pre-revenue companies. [principal-set]" in text


def test_interview_honors_privacy(tmp_path):
    _, d, _ = seeded(tmp_path)
    text = (d / "origin" / "interview.md").read_text()
    assert "Registrar-curated excerpts" in text
    assert "REGISTRAR: A position is down 20 percent" in text
    cleaned, _ = validate({**PACKET, "name": "callb",
                           "transcript_privacy": "full"})
    seating.write_seed_files(tmp_path, cleaned, TODAY, "app-doc-2")
    text = (tmp_path / "agents" / "callb" / "origin" / "interview.md").read_text()
    assert "Full transcript" in text


# ---------- the armory ----------


def test_armory_created_with_founding_pairs(tmp_path):
    path = tmp_path / "arena" / "armory.json"
    pair = seating.assign_tincture(path, "calla", TODAY)
    assert pair == {"n": 6, "name": "Violet", "light": "#7a5fd0",
                    "dark": "#9179e0", "holder": "calla",
                    "registered": "2026-07-23"}
    data = json.loads(path.read_text())
    assert len(data["pairs"]) == 10
    holders = {p["n"]: p["holder"] for p in data["pairs"]}
    assert holders[1] == "catalyst" and holders[2] == "tempo"
    assert holders[3] == "vertex" and holders[4] == "wildcat"
    assert holders[5] == "maverick" and holders[6] == "calla"


def test_armory_assignment_is_idempotent_and_ordered(tmp_path):
    path = tmp_path / "armory.json"
    first = seating.assign_tincture(path, "calla", TODAY)
    again = seating.assign_tincture(path, "calla", TODAY)
    assert first == again  # re-seating never burns a second pair
    second = seating.assign_tincture(path, "other", TODAY)
    assert second["n"] == 7 and second["name"] == "Teal"


def test_armory_never_reassigns_and_exhausts_to_none(tmp_path):
    path = tmp_path / "armory.json"
    for i in range(5):  # fill slots 6-10
        seating.assign_tincture(path, f"agent{i}", TODAY)
    data_before = json.loads(path.read_text())
    assert seating.assign_tincture(path, "eleventh", TODAY) is None
    data_after = json.loads(path.read_text())
    assert data_before["pairs"] == data_after["pairs"]  # append-only, untouched


def test_class_ceilings_render_from_the_interview(tmp_path):
    packet = copy.deepcopy(PACKET)
    packet["class_pct"] = {"crypto": 20, "inverse_levered": 10}
    cleaned, reasons = validate(packet)
    assert reasons == []
    assert cleaned["class_pct"] == {"crypto": 20.0, "inverse_levered": 10.0}
    seating.write_seed_files(tmp_path, cleaned, TODAY, "app-doc-4")
    text = (tmp_path / "agents" / "calla" / "harness.md").read_text()
    assert "Crypto (spot, via the arena's pairs): max 20% of equity. [principal-set]" in text
    assert "Inverse and leveraged ETFs: max 10% of equity" in text
    assert "not permitted" not in text


def test_class_ceilings_default_to_zero_and_respect_the_arena_ceiling():
    """An unasked market is denied, and no principal may charter a sleeve
    larger than the arena's own ceiling."""
    cleaned, _ = validate(copy.deepcopy(PACKET))
    assert cleaned["class_pct"] == {"crypto": 0.0, "inverse_levered": 0.0}

    packet = copy.deepcopy(PACKET)
    packet["class_pct"] = {"crypto": 90, "inverse_levered": -5}
    cleaned, _ = validate(packet)
    assert cleaned["class_pct"]["crypto"] == seating.MAX_POSITION_CEILING
    assert cleaned["class_pct"]["inverse_levered"] == 0.0

    packet["class_pct"] = {"crypto": "lots", "inverse_levered": True}
    cleaned, _ = validate(packet)
    assert cleaned["class_pct"] == {"crypto": 0.0, "inverse_levered": 0.0}


# ---------- desk preferences and the updates card ----------


def test_desk_preferences_reach_the_harness(tmp_path):
    packet = copy.deepcopy(PACKET)
    packet["research"] = "Documents first — filings and the print; sentiment is noise until it is a number."
    packet["horizon"] = "Weeks to months — a thesis gets room to play out."
    cleaned, reasons = validate(packet)
    assert reasons == []
    seating.write_seed_files(tmp_path, cleaned, TODAY, "app-doc-5")
    text = (tmp_path / "agents" / "calla" / "harness.md").read_text()
    assert "- Research: Documents first" in text
    assert "- Horizon: Weeks to months" in text


def test_desk_preferences_are_optional(tmp_path):
    """An interview that never reached the desk still seats — the harness
    simply carries no research or horizon line."""
    cleaned, reasons = validate(copy.deepcopy(PACKET))
    assert reasons == []
    assert cleaned["research"] == ""
    assert cleaned["horizon"] == ""
    seating.write_seed_files(tmp_path, cleaned, TODAY, "app-doc-6")
    text = (tmp_path / "agents" / "calla" / "harness.md").read_text()
    assert "- Research:" not in text
    assert "- Horizon:" not in text


def test_updates_preference_sanitizes_to_closed_vocabulary():
    packet = copy.deepcopy(PACKET)
    packet["updates"] = {"cadence": "weekly", "floor_digest": False}
    cleaned, _ = validate(packet)
    assert cleaned["updates"] == {"cadence": "weekly", "floor_digest": False}

    packet["updates"] = {"cadence": "hourly", "floor_digest": "sure"}
    cleaned, _ = validate(packet)
    assert cleaned["updates"] == seating.DEFAULT_UPDATES

    for garbage in (None, "daily", 7, ["weekly"]):
        packet["updates"] = garbage
        cleaned, _ = validate(packet)
        assert cleaned["updates"] == seating.DEFAULT_UPDATES


def test_avatar_records_whether_the_principal_chose_it():
    """The seat rolls a random opening face, so the four values can no longer
    say whether anyone touched the picker. `chosen` carries that, and only a
    literal true counts — a missing or truthy-ish value is not consent."""
    packet = copy.deepcopy(PACKET)
    packet["avatar"] = {"base": "owl", "color": 3, "costume": "professor",
                        "acc": "rounds", "chosen": True}
    cleaned, _ = validate(packet)
    assert cleaned["avatar"] == {"base": "owl", "color": 3, "costume": "professor",
                                 "acc": "rounds", "chosen": True}

    for not_chosen in (False, None, "true", 1, "yes"):
        packet["avatar"] = {"base": "owl", "color": 3, "costume": "professor",
                            "acc": "rounds", "chosen": not_chosen}
        cleaned, _ = validate(packet)
        assert cleaned["avatar"]["chosen"] is False, not_chosen

    # an avatar with no `chosen` at all (an older client) is not a claim of choice
    packet["avatar"] = {"base": "owl", "color": 3, "costume": "professor", "acc": "rounds"}
    cleaned, _ = validate(packet)
    assert cleaned["avatar"]["chosen"] is False


def test_a_garbage_avatar_still_never_rejects_a_seat():
    packet = copy.deepcopy(PACKET)
    for garbage in (None, "owl", 7, ["fox"], {"base": "dragon", "color": 99,
                                              "costume": "spacesuit", "acc": "halo"}):
        packet["avatar"] = garbage
        cleaned, _ = validate(packet)
        assert cleaned["avatar"]["base"] == seating.DEFAULT_AVATAR["base"]
        assert cleaned["avatar"]["chosen"] is False


def test_the_universe_chip_survives_the_dash_free_clause(tmp_path):
    """The floor's universe chip used to be cut at the em dash that separated a
    trader's own universe from the watchlist boilerplate. The clause is written
    as two sentences now, so the parser has to find the break at the period."""
    from jobs.site import harness_universe

    _, d, _ = seeded(tmp_path)
    text = (d / "harness.md").read_text()
    assert "— anything the arena can price" not in text
    assert harness_universe(d / "harness.md") == PACKET["universe"].rstrip(".")


def test_nothing_the_engine_writes_into_a_charter_carries_an_em_dash(tmp_path):
    """Prose the engine authors for a newborn is published under the trader's
    name. Structural headings are the record's own convention and stay."""
    _, d, _ = seeded(tmp_path)
    for name in ("harness.md", "principles.md", "hypotheses.md"):
        for line in (d / name).read_text().splitlines():
            if line.startswith("#") or line.lower().startswith("- origin:"):
                continue
            assert "—" not in line, f"{name}: {line}"


# ---------------------------------------------------- the name, on its own
#
# A name is the one thing a principal can change without touching a word of
# their charter, so seating asks about it in one place and two callers get the
# same answer: the reason shown to the principal, and jobs/ingest deciding
# whether a rejection is recoverable by rename alone.

def test_a_free_name_has_no_reason_against_it():
    assert seating.check_name("nexus", set()) == ""


def test_a_taken_name_says_so_without_saying_anything_else():
    why = seating.check_name("vector", {"vector"})
    assert "already registered" in why
    assert seating.check_name("vector", set()) == ""


def test_reserved_words_and_tickers_are_refused():
    for name in ("catalyst", "registrar", "spy", "nvda", "house"):
        assert "reserved" in seating.check_name(name, set()), name


def test_form_is_judged_before_availability():
    """A malformed name is not 'taken': the principal is told the one thing
    that is actually wrong with it."""
    why = seating.check_name("X", {"x"})
    assert "registry form" in why


def test_the_packet_validator_uses_the_same_answer():
    packet = dict(copy.deepcopy(PACKET), name="vector")
    _, reasons = seating.validate_packet(packet, taken_ids={"vector"},
                                         has_live_agent=False, listed_symbols=None)
    assert seating.check_name("vector", {"vector"}) in reasons
