"""The desk's channel into the record: what the engine will and will not accept
as an answer to a principal's note, and how notes are written down."""
from jobs import guidance
from runner import ops

AGENT = {"id": "ballast", "config": {}}


class FakeConn:
    """Just enough connection for a dry run: the guidance lookup."""

    def __init__(self, pending=("C1",)):
        self.pending = set(pending)
        self.last = None

    def execute(self, sql, params=None):
        self.last = (sql, params)
        return self

    def fetchone(self):
        sql, params = self.last
        if "from guidance" in sql:
            cid = params[1]
            return {"id": 7} if cid in self.pending else None
        return None

    def commit(self):
        pass


def apply(op, extra=(), pending=("C1",)):
    conn = FakeConn(pending)
    all_ops = [op, *extra]
    results = ops.validate_and_apply(conn, AGENT, None, all_ops, dry=True)
    return results[0][1], results[0][2]


def test_answer_to_a_waiting_note_is_accepted():
    verdict, reason = apply({
        "type": "guidance_response", "cid": "C1", "disposition": "declined",
        "note": "MRK is on my watching-list, but P4 forbids me to trade motion.",
    })
    assert verdict == "accepted" and "declined" in reason


def test_answer_to_a_note_that_is_not_waiting_is_rejected():
    verdict, reason = apply({
        "type": "guidance_response", "cid": "C9", "disposition": "adopted",
        "note": "Added to the watching-list as you asked, and priced at the close.",
    })
    assert verdict == "rejected" and "no unanswered guidance" in reason


def test_disposition_must_be_one_of_the_four():
    verdict, reason = apply({
        "type": "guidance_response", "cid": "C1", "disposition": "noted",
        "note": "I have read it and will think about it at some point.",
    })
    assert verdict == "rejected" and "adopted, converted, declined or refused" in reason


def test_a_label_is_not_an_answer():
    verdict, reason = apply({
        "type": "guidance_response", "cid": "C1", "disposition": "declined",
        "note": "no",
    })
    assert verdict == "rejected" and "own words" in reason


def test_converted_must_actually_file_the_test():
    op = {"type": "guidance_response", "cid": "C1", "disposition": "converted",
          "note": "You believe semis lead the recovery; let us make that testable."}
    verdict, reason = apply(op)
    assert verdict == "rejected" and "same operations block" in reason
    verdict, _ = apply(op, extra=[{"type": "hypothesis_op", "op": "propose", "id": "H2"}])
    assert verdict == "accepted"


# ---------- taking a note in ----------

class FakeRef:
    def __init__(self):
        self.updates = []

    def update(self, d):
        self.updates.append(d)


class FakeDoc:
    def __init__(self, data, doc_id="doc-1"):
        self.id = doc_id
        self._data = data
        self.reference = FakeRef()

    def to_dict(self):
        return self._data


class IngestConn:
    """Agent row + counters, enough for jobs.guidance.take()."""

    def __init__(self, agent=None, today_count=0, total=0):
        self.agent = agent
        self.today_count = today_count
        self.total = total
        self.inserts = []
        self.last = None

    def execute(self, sql, params=None):
        self.last = (sql, params)
        if "insert into guidance" in sql:
            self.inserts.append(params)
        return self

    def fetchone(self):
        sql, _ = self.last
        if "from agents" in sql:
            return self.agent
        if "filed_at::date" in sql:
            return {"c": self.today_count}
        if "count(*) c from guidance" in sql:
            return {"c": self.total}
        return None

    def commit(self):
        pass


BALLAST = {"id": "ballast", "name": "Ballast", "owner_uid": "uid-1", "status": "active"}
TODAY = "2026-07-28"


def take(tmp_path, monkeypatch, data, conn):
    monkeypatch.setattr(guidance, "TRADER", tmp_path)
    doc = FakeDoc(data)
    path = guidance.take(conn, doc, TODAY)
    return doc, path


def test_a_note_from_the_owner_is_filed(tmp_path, monkeypatch):
    conn = IngestConn(agent=BALLAST, total=1)
    doc, path = take(tmp_path, monkeypatch,
                     {"uid": "uid-1", "trader": "ballast", "text": "Look at MRK."}, conn)
    assert conn.inserts and conn.inserts[0][:2] == ("ballast", "C2")  # ids continue
    assert doc.reference.updates[0]["status"] == "ingested"
    assert doc.reference.updates[0]["cid"] == "C2"
    assert "> Look at MRK." in path.read_text()


def test_a_note_for_someone_elses_trader_is_never_filed(tmp_path, monkeypatch):
    conn = IngestConn(agent=BALLAST)
    doc, path = take(tmp_path, monkeypatch,
                     {"uid": "someone-else", "trader": "ballast", "text": "Sell it all."}, conn)
    assert path is None and not conn.inserts
    assert doc.reference.updates[0]["status"] == "rejected"
    assert "does not answer to this account" in doc.reference.updates[0]["reason"]


def test_the_days_limit_is_enforced_by_the_engine(tmp_path, monkeypatch):
    conn = IngestConn(agent=BALLAST, today_count=guidance.PER_DAY)
    doc, path = take(tmp_path, monkeypatch,
                     {"uid": "uid-1", "trader": "ballast", "text": "One more thing."}, conn)
    assert path is None and not conn.inserts
    assert "3 notes a day" in doc.reference.updates[0]["reason"]


def test_an_empty_note_is_not_a_note(tmp_path, monkeypatch):
    conn = IngestConn(agent=BALLAST)
    doc, path = take(tmp_path, monkeypatch,
                     {"uid": "uid-1", "trader": "ballast", "text": "   "}, conn)
    assert path is None and doc.reference.updates[0]["status"] == "rejected"


# ---------- the record file ----------

def test_guidance_file_is_append_only(tmp_path, monkeypatch):
    monkeypatch.setattr(guidance, "TRADER", tmp_path)
    (tmp_path / "agents" / "ballast").mkdir(parents=True)
    guidance.append_entry("ballast", "Ballast", "C1 · filed 2026-07-28",
                          "> Why are you still all in cash?")
    first = (tmp_path / "agents" / "ballast" / "guidance.md").read_text()
    guidance.append_entry("ballast", "Ballast", "C1 · answered 2026-07-28 — declined",
                          "P4 forbids me to trade motion. Standing pat, and saying why.")
    text = (tmp_path / "agents" / "ballast" / "guidance.md").read_text()

    assert text.startswith("# Ballast — guidance")
    assert text.count("# Ballast — guidance") == 1          # header written once
    assert text.startswith(first.rstrip("\n"))              # nothing rewritten
    assert "## C1 · filed 2026-07-28" in text
    assert "## C1 · answered 2026-07-28 — declined" in text
    assert text.index("filed 2026-07-28") < text.index("answered 2026-07-28")
