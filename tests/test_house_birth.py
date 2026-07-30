"""Batch-two house agents: born by sync_house_config, prose first, rows second."""
import json

from jobs import sync_house_config as shc
from jobs.seed import AGENTS

BATCH2 = {aid for aid, m in AGENTS.items() if "bench" in m}


class FakeConn:
    def __init__(self):
        self.sql = []
        self.commits = 0

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        self._last = sql
        return self

    def fetchone(self):
        return None  # nothing exists yet — every agent is missing

    def commit(self):
        self.commits += 1


def test_batch_two_is_ten_agents_with_benches():
    assert len(BATCH2) == 10
    for aid in BATCH2:
        b = AGENTS[aid]["bench"]
        assert abs(sum(b["weights"]) - 1.0) < 1e-6, f"{aid} weights must sum to 1"
        assert len(b["symbols"]) == len(b["weights"])
        av = AGENTS[aid]["config"]["avatar"]
        assert set(av) == {"base", "color", "costume", "acc"}


def test_avatars_are_distinct_from_each_other_and_the_house():
    house = [("hawk", 4, "gilet"), ("fox", 0, "suit"), ("owl", 3, "professor"),
             ("bull", 1, "pit"), ("shark", 2, "hoodie")]
    seen = set(house)
    for aid in BATCH2:
        av = AGENTS[aid]["config"]["avatar"]
        key = (av["base"], av["color"], av["costume"])
        assert key not in seen, f"{aid} wears an already-worn member: {key}"
        seen.add(key)


def test_birth_refuses_without_prose(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(shc, "TRADER", tmp_path)  # no agents/<id>/harness.md
    conn = FakeConn()
    assert shc.birth(conn, "drift", AGENTS["drift"], dry=False) is False
    assert conn.sql == [], "no rows may exist for an agent with no charter"
    assert "REFUSED" in capsys.readouterr().out


def test_birth_inserts_rows_and_takes_a_tincture(tmp_path, monkeypatch):
    (tmp_path / "agents" / "drift").mkdir(parents=True)
    (tmp_path / "agents" / "drift" / "harness.md").write_text("# Drift — harness")
    monkeypatch.setattr(shc, "TRADER", tmp_path)
    conn = FakeConn()
    assert shc.birth(conn, "drift", AGENTS["drift"], dry=False) is True
    sqls = [s for s, _ in conn.sql]
    assert any("insert into agents" in s for s in sqls)
    assert any("insert into agent_state" in s for s in sqls)
    _, agent_params = conn.sql[0]
    assert agent_params[0] == "drift"
    cfg = json.loads(agent_params[4])
    assert cfg["max_single_pct"] == 0.30 and cfg["avatar"]["base"] == "stag"
    _, state_params = conn.sql[1]
    bench = json.loads(state_params[3])
    assert bench["symbols"] == ["SPY"] and bench["launch_prices"] == []
    # a tincture was registered in the (temp) armory
    armory = json.loads((tmp_path / "arena" / "armory.json").read_text())
    assert any(p.get("holder") == "drift" for p in armory["pairs"])


def test_birth_is_dry_run_safe(tmp_path, monkeypatch):
    (tmp_path / "agents" / "gale").mkdir(parents=True)
    (tmp_path / "agents" / "gale" / "harness.md").write_text("# Gale — harness")
    monkeypatch.setattr(shc, "TRADER", tmp_path)
    conn = FakeConn()
    assert shc.birth(conn, "gale", AGENTS["gale"], dry=True) is True
    assert conn.sql == []
    assert not (tmp_path / "arena").exists(), "dry run must not touch the armory"


def test_founding_agents_are_never_born_here(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(shc, "TRADER", tmp_path)
    conn = FakeConn()
    assert shc.birth(conn, "tempo", AGENTS["tempo"], dry=False) is False
    assert conn.sql == []
    assert "jobs.seed" in capsys.readouterr().out


def test_the_aggressive_spectrum_rulings_are_in_the_configs():
    caps = {aid: AGENTS[aid]["config"] for aid in BATCH2}
    # every cap inside the 25–40% ruling
    for aid, c in caps.items():
        assert 0.25 <= c["max_single_pct"] <= 0.40, aid
    # exactly two crypto sleeves (ember 30%, surge 15%)
    crypto = {aid: c["class_caps"].get("crypto", 0) for aid, c in caps.items()}
    assert crypto["ember"] == 0.30 and crypto["surge"] == 0.15
    assert sum(1 for v in crypto.values() if v) == 2
    # the two levered books carry the 40% sleeve, forge its 25%
    lev = {aid: c["class_caps"].get("inverse_levered", 0) for aid, c in caps.items()}
    assert lev["gale"] == 0.40 and lev["talon"] == 0.40 and lev["forge"] == 0.25
    assert all(v == 0.15 for aid, v in lev.items()
               if aid not in ("gale", "talon", "forge"))
