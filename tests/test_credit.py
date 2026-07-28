"""Who is named on the floor: the principal's display preference, carried from
Firestore into the config the floor is built from. Anonymous is the default,
the fallback, and what a failure leaves behind."""
import json

from jobs import credit


class FakeSnap:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return self._data


class FakeFS:
    """users/{uid} lookups only. A uid mapped to an Exception raises on read."""

    def __init__(self, users):
        self.users = users
        self.reads = 0

    def collection(self, name):
        assert name == "users"
        return self

    def document(self, uid):
        self._uid = uid
        return self

    def get(self):
        self.reads += 1
        v = self.users.get(self._uid, None)
        if isinstance(v, Exception):
            raise v
        return FakeSnap(v)


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.writes = []
        self.committed = False

    def execute(self, sql, params=None):
        if sql.strip().startswith("update"):
            self.writes.append((params[1], json.loads(params[0])))
        return self

    def fetchall(self):
        return self.rows

    def commit(self):
        self.committed = True


def row(aid, uid, config=None):
    return {"id": aid, "owner_uid": uid, "config": config or {}}


def test_a_name_is_cleaned_to_one_printable_line():
    assert credit.clean_name("  Tom   Haramaty \n") == "Tom Haramaty"
    assert credit.clean_name("TomHaramaty") == "Tom Haramaty"
    assert len(credit.clean_name("x" * 200)) == credit.MAX_NAME


def test_a_name_is_never_markup():
    assert credit.clean_name("Tom <script>alert(1)</script>") == "Tom script alert(1) /script"


def test_a_name_that_carries_a_link_is_no_name():
    assert credit.clean_name("Tom — cheapstocks.com") == ""
    assert credit.clean_name("https://spam.example") == ""
    assert credit.clean_name("www.spam.io") == ""


def test_silence_is_anonymous():
    assert credit.credit_of(None) == credit.ANON
    assert credit.credit_of(FakeSnap(None)) == credit.ANON
    assert credit.credit_of(FakeSnap({"email": "a@b.c"})) == credit.ANON


def test_a_name_is_carried_only_when_the_principal_asked_for_it():
    named = FakeSnap({"credit": {"name": "Tom Haramaty", "show": True}})
    assert credit.credit_of(named) == {"name": "Tom Haramaty", "show": True}
    hidden = FakeSnap({"credit": {"name": "Tom Haramaty", "show": False}})
    assert credit.credit_of(hidden) == credit.ANON
    empty = FakeSnap({"credit": {"name": "   ", "show": True}})
    assert credit.credit_of(empty) == credit.ANON


def test_sync_writes_the_preference_into_config_and_keeps_the_rest():
    conn = FakeConn([row("ballast", "u1", {"max_single_pct": 0.2})])
    fs = FakeFS({"u1": {"credit": {"name": "Tom Haramaty", "show": True}}})
    changed, failed = credit.sync(conn, fs)
    assert (changed, failed) == (1, 0) and conn.committed
    aid, config = conn.writes[0]
    assert aid == "ballast"
    assert config["credit"] == {"name": "Tom Haramaty", "show": True}
    assert config["max_single_pct"] == 0.2


def test_turning_it_off_writes_the_name_back_out_of_the_config():
    conn = FakeConn([row("ballast", "u1",
                         {"credit": {"name": "Tom Haramaty", "show": True}})])
    fs = FakeFS({"u1": {"credit": {"name": "Tom Haramaty", "show": False}}})
    changed, _ = credit.sync(conn, fs)
    assert changed == 1 and conn.writes[0][1]["credit"] == credit.ANON


def test_an_unchanged_preference_writes_nothing():
    conn = FakeConn([row("ballast", "u1",
                         {"credit": {"name": "Tom Haramaty", "show": True}})])
    fs = FakeFS({"u1": {"credit": {"name": "Tom Haramaty", "show": True}}})
    changed, _ = credit.sync(conn, fs)
    assert changed == 0 and conn.writes == []


def test_one_profile_is_read_once_however_many_traders_it_holds():
    conn = FakeConn([row("ballast", "u1"), row("rapid", "u1")])
    fs = FakeFS({"u1": {"credit": {"name": "Tom Haramaty", "show": True}}})
    changed, _ = credit.sync(conn, fs)
    assert changed == 2 and fs.reads == 1


def test_an_unreadable_profile_leaves_the_config_alone():
    conn = FakeConn([row("ballast", "u1",
                         {"credit": {"name": "Tom Haramaty", "show": True}})])
    fs = FakeFS({"u1": RuntimeError("firestore is down")})
    changed, failed = credit.sync(conn, fs)
    assert (changed, failed) == (0, 1) and conn.writes == []


def test_dry_run_writes_nothing():
    conn = FakeConn([row("ballast", "u1")])
    fs = FakeFS({"u1": {"credit": {"name": "Tom Haramaty", "show": True}}})
    changed, _ = credit.sync(conn, fs, dry=True)
    assert changed == 1 and conn.writes == [] and not conn.committed
