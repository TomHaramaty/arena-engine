import copy
import json
from datetime import date

from engine import seating
from jobs.site import parse_hypotheses, parse_principles

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
    assert any("not on the arena quote sheet" in r for r in reasons)
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
                    "## Constitution (hard limits — cannot be changed by reflection)",
                    "## Parameters"):
        assert section in text
    # arena floor, always present
    assert "Long-only. No leverage, no derivatives" in text
    assert "arena quote sheet symbols only" in text
    assert "watchlist requests" in text
    assert "written thesis with invalidation conditions" in text
    assert "Simulated fills only, per arena protocol." in text
    # principal-set pieces
    assert "Max single position: 25% of equity at cost. [principal-set]" in text
    assert "Never average down into a losing position. [principal-set]" in text
    assert "Benchmark: SPY (SPY)." in text
    assert "skeptical, plain-spoken" in text


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
