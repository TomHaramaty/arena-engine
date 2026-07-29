"""The sandbox and the purge: what a test seating is, and what deletion will
and will not do. See design/account-deletion-2026-07-29.md in the trader repo."""
import pathlib

from datetime import date

import pytest

from engine import sandbox
from tests.test_seating import TODAY
from jobs import purge


# ---------- the sandbox ----------

def test_admin_uid_is_recognised(monkeypatch):
    monkeypatch.setenv("ADMIN_UIDS", "abc123, def456")
    assert sandbox.is_admin("abc123") and sandbox.is_admin("def456")
    assert not sandbox.is_admin("someone-else")
    assert not sandbox.is_admin("") and not sandbox.is_admin(None)


def test_admin_uids_fall_back_to_the_constant(monkeypatch):
    monkeypatch.delenv("ADMIN_UIDS", raising=False)
    assert sandbox.admin_uids() == set(sandbox.DEFAULT_ADMIN_UIDS)


def test_agent_dir_is_the_record_by_default(tmp_path):
    assert sandbox.agent_dir(tmp_path, "ballast") == tmp_path / "agents" / "ballast"


def test_agent_dir_follows_an_existing_sandbox_tree(tmp_path):
    sb = tmp_path / "sandbox" / "agents" / "probe"
    sb.mkdir(parents=True)
    assert sandbox.agent_dir(tmp_path, "probe") == sb


def test_seeding_can_choose_the_sandbox_before_it_exists(tmp_path):
    assert (sandbox.agent_dir(tmp_path, "probe", sandbox=True)
            == tmp_path / "sandbox" / "agents" / "probe")


def test_in_sandbox_guards_the_git_callers(tmp_path):
    assert sandbox.in_sandbox(tmp_path / "sandbox" / "agents" / "probe" / "x.md")
    assert not sandbox.in_sandbox(tmp_path / "agents" / "ballast" / "x.md")


def test_seed_files_land_in_the_sandbox(tmp_path):
    from engine import seating
    written = seating.write_seed_files(tmp_path, _cleaned(), TODAY, "app1",
                                       sandbox_seat=True)
    assert written, "seeding wrote nothing"
    for p in written:
        assert sandbox.in_sandbox(p), f"{p} escaped the sandbox"
    assert not (tmp_path / "agents").exists(), "the record was touched"


def test_seed_files_land_in_the_record_when_not_sandboxed(tmp_path):
    from engine import seating
    seating.write_seed_files(tmp_path, _cleaned(), TODAY, "app1")
    assert (tmp_path / "agents" / "calla" / "harness.md").exists()
    assert not (tmp_path / "sandbox").exists()


def _cleaned():
    from tests.test_seating import PACKET, validate
    cleaned, reasons = validate(PACKET)
    assert not reasons, reasons
    return cleaned


# ---------- the purge ----------

class FakeConn:
    """Counts by table, and records every statement executed."""

    def __init__(self, tier="test", counts=None):
        self.tier = tier
        self.counts = counts or {}
        self.sql = []
        self._last = None
        self.commits = 0

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        self._last = (sql, params)
        return self

    def fetchone(self):
        sql, params = self._last
        if "from agents where id" in sql:
            return {"id": "probe", "name": "probe", "tier": self.tier,
                    "status": "active", "owner_uid": "uid-1"}
        if "count(*)" in sql:
            for t, c in self.counts.items():
                if f"from {t} " in sql or f"join runs" in sql and t == "operations":
                    return {"c": c}
            return {"c": 0}
        return None

    def commit(self):
        self.commits += 1


def test_a_real_trader_cannot_be_purged(monkeypatch):
    conn = FakeConn(tier="seated")
    monkeypatch.setattr(purge.db, "connect", lambda: conn)
    with pytest.raises(SystemExit) as e:
        purge.main(["--trader", "probe", "--yes"])
    assert "--retire" in str(e.value)
    assert not any("delete" in s.lower() for s, _ in conn.sql)


def test_a_real_trader_cannot_be_purged_even_with_auth(monkeypatch):
    conn = FakeConn(tier="house")
    monkeypatch.setattr(purge.db, "connect", lambda: conn)
    with pytest.raises(SystemExit):
        purge.main(["--trader", "probe", "--yes", "--auth"])
    assert not any("delete" in s.lower() for s, _ in conn.sql)


def test_dry_run_writes_nothing(monkeypatch, capsys):
    conn = FakeConn(counts={"runs": 3, "fills": 2})
    monkeypatch.setattr(purge.db, "connect", lambda: conn)
    monkeypatch.setattr(purge, "fs_docs", lambda *a: [])
    assert purge.main(["--trader", "probe"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "PURGE" in out
    assert not any("delete" in s.lower() for s, _ in conn.sql)
    assert conn.commits == 0


def test_purge_deletes_children_before_parents(monkeypatch, tmp_path):
    conn = FakeConn()
    monkeypatch.setattr(purge.db, "connect", lambda: conn)
    monkeypatch.setattr(purge, "fs_docs", lambda *a: [])
    monkeypatch.setattr(purge, "TRADER", tmp_path)
    purge.main(["--trader", "probe", "--yes"])
    deletes = [s for s, _ in conn.sql if s.lower().startswith("delete")]
    order = [next(t for t in (*purge.AGENT_TABLES, "operations", "agents")
                  if f"delete from {t} " in s) for s in deletes]
    assert order[0] == "operations"
    assert order[-1] == "agents"
    assert order.index("orders") < order.index("agents")
    assert order.index("fills") < order.index("orders")


def test_purge_never_touches_shared_data(monkeypatch, tmp_path):
    conn = FakeConn()
    monkeypatch.setattr(purge.db, "connect", lambda: conn)
    monkeypatch.setattr(purge, "fs_docs", lambda *a: [])
    monkeypatch.setattr(purge, "TRADER", tmp_path)
    purge.main(["--trader", "probe", "--yes"])
    joined = " ".join(s for s, _ in conn.sql)
    assert "watchlist" not in joined and "ticks" not in joined


def test_purge_removes_the_sandbox_tree_only(monkeypatch, tmp_path):
    sb = tmp_path / "sandbox" / "agents" / "probe"
    (sb / "journal").mkdir(parents=True)
    (sb / "harness.md").write_text("x")
    record = tmp_path / "agents" / "ballast"
    record.mkdir(parents=True)
    (record / "harness.md").write_text("the record")

    conn = FakeConn()
    monkeypatch.setattr(purge.db, "connect", lambda: conn)
    monkeypatch.setattr(purge, "fs_docs", lambda *a: [])
    monkeypatch.setattr(purge, "TRADER", tmp_path)
    purge.main(["--trader", "probe", "--yes"])
    assert not sb.exists()
    assert (record / "harness.md").read_text() == "the record"


def test_purge_refuses_a_prose_dir_outside_the_sandbox(monkeypatch, tmp_path):
    """The tier gate is the real guard; this is the belt to its braces — if a
    test trader somehow has record prose, the tree is not deleted."""
    d = tmp_path / "agents" / "probe"
    d.mkdir(parents=True)
    (d / "harness.md").write_text("x")
    conn = FakeConn()
    monkeypatch.setattr(purge.db, "connect", lambda: conn)
    monkeypatch.setattr(purge, "fs_docs", lambda *a: [])
    monkeypatch.setattr(purge, "TRADER", tmp_path)
    with pytest.raises(SystemExit) as e:
        purge.main(["--trader", "probe", "--yes"])
    assert "refusing" in str(e.value)
    assert (d / "harness.md").exists()
    # and it refused BEFORE the deletes — a half-purged trader is worse than either
    assert not any(s.lower().startswith("delete") for s, _ in conn.sql)


# ---------- retirement ----------

def test_retire_deletes_no_rows_and_no_prose(monkeypatch, tmp_path):
    d = tmp_path / "agents" / "probe" / "journal"
    d.mkdir(parents=True)
    (d / "2026-07-28.md").write_text("a real entry")
    conn = FakeConn(tier="seated")
    monkeypatch.setattr(purge.db, "connect", lambda: conn)
    monkeypatch.setattr(purge, "fs_docs", lambda *a: [])
    monkeypatch.setattr(purge, "TRADER", tmp_path)
    purge.main(["--trader", "probe", "--retire", "--yes"])
    assert not any(s.lower().startswith("delete") for s, _ in conn.sql)
    assert (d / "2026-07-28.md").read_text() == "a real entry"
    assert any("update agents set status='withdrawn'" in s for s, _ in conn.sql)


def test_retire_appends_a_withdrawal_entry(tmp_path):
    (tmp_path / "agents" / "probe" / "journal").mkdir(parents=True)
    p = purge_with_root(tmp_path, "probe", "2026-07-29")
    assert p.name == "2026-07-29-withdrawn.md"
    body = p.read_text()
    assert "withdrew this trader on 2026-07-29" in body
    assert "Nothing above this line has been altered" in body
    # idempotent: a second retirement writes nothing new
    assert purge_with_root(tmp_path, "probe", "2026-07-29") is None


def purge_with_root(root, aid, today):
    import engine.sandbox as sb_mod
    old = purge.TRADER
    purge.TRADER = pathlib.Path(root)
    try:
        return purge.write_withdrawal(aid, today)
    finally:
        purge.TRADER = old


def test_retire_keeps_the_profile_and_the_applications():
    assert purge.keep_for_retire("users/uid-1")
    assert purge.keep_for_retire("applications/abc")
    assert not purge.keep_for_retire("desks/uid-1_probe")
    assert not purge.keep_for_retire("drafts/uid-1")
    assert not purge.keep_for_retire("guidance/xyz")


# ---------- seating into the sandbox (the ingest's decision) ----------

class SeatConn:
    """Records the inserts a seating makes."""

    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def commit(self):
        pass


def _seat(monkeypatch, tmp_path, uid):
    from jobs import ingest
    monkeypatch.setattr(ingest, "TRADER", tmp_path)
    conn = SeatConn()
    paths, pair = ingest.seat(conn, _cleaned(), uid, "app-1", TODAY)
    tier = next(p[-2] for s, p in conn.sql if "insert into agents" in s)
    return conn, paths, pair, tier


def test_an_admin_seats_into_the_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_UIDS", "admin-uid")
    conn, paths, pair, tier = _seat(monkeypatch, tmp_path, "admin-uid")
    assert tier == sandbox.TIER
    assert pair is None, "a test trader must never hold a tincture"
    assert not (tmp_path / "arena" / "armory.json").exists()
    assert paths and all(sandbox.in_sandbox(p) for p in paths)
    assert not (tmp_path / "agents").exists()


def test_anyone_else_seats_onto_the_floor(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_UIDS", "admin-uid")
    conn, paths, pair, tier = _seat(monkeypatch, tmp_path, "a-real-principal")
    assert tier == "seated"
    assert pair and pair["holder"] == "calla", "a real trader is registered a colour"
    assert (tmp_path / "agents" / "calla" / "harness.md").exists()
    assert not (tmp_path / "sandbox").exists()


def test_the_sandbox_is_never_committed(monkeypatch, tmp_path, capsys):
    """commit_trader is the one thing standing between a test seating and the
    record's git history — in CI it would otherwise push."""
    from jobs import ingest
    monkeypatch.setattr(ingest, "TRADER", tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    calls = []
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: calls.append(a))
    ingest.commit_trader([tmp_path / "sandbox" / "agents" / "probe" / "harness.md"],
                         "probe", TODAY)
    assert calls == []
