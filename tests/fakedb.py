"""A book in memory, for testing the layers that touch the database.

The engine's highest-stakes code is not the pure arithmetic — that was already
tested — it is the layer that decides what a brain is ALLOWED to do and then
moves the book: runner/ops.validate_and_apply. On 2026-07-30 that file had 338
lines, every constitutional rejection in the arena, and zero tests, because
testing it appeared to need Postgres.

It does not. It needs a connection that routes the two dozen statements the
engine actually issues and keeps an honest book while it does. That is what this
is: not a SQL engine, a ledger with a SQL-shaped door.

What it models faithfully, because the tests turn on it:
  · cash, positions (qty + weighted avg_fill), ticks, watchlist, orders, guidance
  · the EFFECTS of the engine's writes, applied in order — so a batch that buys
    twice sees its own first buy when the second one's capacity is computed,
    which is the property that stops an agent slicing through a cap
  · every write, logged verbatim, so a test can assert on what reached the record

What it deliberately does not model: SQL itself. An unrecognised statement raises
rather than quietly returning no rows — a fake that answers "nothing" to a
question it did not understand turns a real bug into a passing test.
"""
import json
import re


class FakeSQLError(AssertionError):
    """A statement this fake does not know. Teach it, never ignore it."""


def _norm(sql):
    return " ".join(str(sql).split()).lower()


class Result:
    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    # core.py uses `with conn.cursor() as cur: cur.execute(...)`
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, *, cash=100_000.0, positions=None, ticks=None,
                 watchlist=None, orders=None, guidance=None, agents=None,
                 agent_id="tempo", config=None, marks=None, triggers=None,
                 peak=None, bench=None):
        self.agent_id = agent_id
        self.cash = float(cash)
        self.peak_equity = float(peak if peak is not None else cash)
        self.bench = bench or {"symbols": ["SPY"], "weights": [1.0],
                               "launch_prices": [500.0]}
        # {symbol: {"qty": float, "avg_fill": float}}
        self.positions = {k: dict(v) for k, v in (positions or {}).items()}
        self.ticks = dict(ticks or {})
        # {symbol: asset_class}
        self.watchlist = dict(watchlist or {})
        self.orders = [dict(o) for o in (orders or [])]
        self.guidance = [dict(g) for g in (guidance or [])]
        self.agents = agents or {agent_id: (config or {})}
        self.runs = []                          # [{id, agent_id, status, meta}]
        self.traded_sessions = []               # core.flag_dormancy, newest first
        self.last_reflection = None
        self.marks = list(marks or [])          # [{agent_id, ts, equity}]
        self.triggers = list(triggers or [])    # [{agent_id, kind, details, ts, handled}]
        self.writes = []                        # (normalized_sql, args)
        self.operations = []                    # (type, verdict, reason)
        self.commits = 0
        self._next_order_id = 1 + max([o.get("id", 0) for o in self.orders] or [0])

    # ---------- the door ----------

    def execute(self, sql, args=None):
        s, a = _norm(sql), tuple(args or ())
        self.writes.append((s, a))
        for pattern, handler in self._routes():
            if s.startswith(pattern):
                return Result(handler(s, a) or [])
        raise FakeSQLError(f"fakedb does not know this statement:\n  {sql}")

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    # ---------- assertions a test wants ----------

    def wrote(self, prefix):
        p = _norm(prefix)
        return [(s, a) for s, a in self.writes if s.startswith(p)]

    def position_qty(self, symbol):
        return float(self.positions.get(symbol, {}).get("qty", 0.0))

    def equity(self):
        return self.cash + sum(
            p["qty"] * self.ticks.get(s, 0.0) for s, p in self.positions.items()
        )

    # ---------- routing ----------

    def _routes(self):
        return (
            # reads
            ("select price from ticks", self._r_price),
            ("select distinct on (symbol) symbol, price from ticks", self._r_all_prices),
            ("select qty from positions where agent_id=%s and symbol=%s", self._r_pos_qty),
            ("select symbol, qty from positions where agent_id=", self._r_positions),
            ("select distinct symbol from positions", self._r_pos_symbols),
            ("select 1 from watchlist where symbol=", self._r_watchlisted),
            ("select symbol, asset_class from watchlist", self._r_classes),
            ("select cash from agent_state where agent_id=", self._r_cash),
            ("select a.id, s.cash, s.peak_equity, s.bench", self._r_states),
            ("select agent_id, bench from agent_state where launched is null",
             self._r_unlaunched),
            ("select * from orders where status='open'", self._r_open_orders),
            ("select id from orders where id=", self._r_own_open_order),
            ("select id, config from agents", self._r_agent_configs),
            ("select * from agents where id=", self._r_agent_row),
            ("insert into runs", self._w_run),
            ("update runs set", self._w_run_update),
            ("update triggers_fired set handled=true", self._w_handle_triggers),
            ("select id from guidance where agent_id=", self._r_guidance),
            ("select ts, details, handled from triggers_fired", self._r_last_trigger),
            ("select exists (", self._r_traded_flags),
            ("select max(started) t from runs", self._r_last_reflection),
            ("select 1 from triggers_fired", self._r_kind_filed),
            ("select 1 from equity_marks", self._r_recovered),
            ("select cid from guidance where agent_id=", self._r_guidance_cids),
            # writes
            ("insert into orders", self._w_order),
            ("insert into fills", self._w_noop),
            ("insert into positions", self._w_position),
            ("insert into triggers_fired", self._w_trigger),
            ("insert into operations", self._w_operation),
            ("insert into ticks", self._w_noop),
            ("insert into equity_marks", self._w_mark),
            ("update agent_state set cash", self._w_cash),
            ("update agent_state set peak_equity", self._w_peak),
            ("update positions set qty", self._w_pos_qty),
            ("delete from positions", self._w_pos_delete),
            ("update orders set", self._w_order_status),
            ("update guidance set", self._w_guidance),
            ("insert into watchlist", self._w_watchlist),
        )

    # reads -------------------------------------------------------------

    def _r_price(self, s, a):
        sym = a[0]
        return [{"price": self.ticks[sym]}] if sym in self.ticks else []

    def _r_all_prices(self, s, a):
        return [{"symbol": k, "price": v} for k, v in self.ticks.items()]

    def _r_pos_qty(self, s, a):
        sym = a[1]
        return [{"qty": self.positions[sym]["qty"]}] if sym in self.positions else []

    def _r_positions(self, s, a):
        return [{"symbol": k, "qty": v["qty"]} for k, v in self.positions.items()]

    def _r_pos_symbols(self, s, a):
        return [{"symbol": k} for k in self.positions]

    def _r_watchlisted(self, s, a):
        return [{"?column?": 1}] if a[0] in self.watchlist else []

    def _r_classes(self, s, a):
        return [{"symbol": k, "asset_class": v} for k, v in self.watchlist.items()]

    def _r_cash(self, s, a):
        return [{"cash": self.cash}]

    def _r_states(self, s, a):
        return [{"id": self.agent_id, "cash": self.cash,
                 "peak_equity": self.peak_equity, "bench": self.bench}]

    def _r_unlaunched(self, s, a):
        return []

    def _r_open_orders(self, s, a):
        return [o for o in self.orders if o.get("status", "open") == "open"]

    def _r_own_open_order(self, s, a):
        oid, aid = a[0], a[1]
        return [{"id": o["id"]} for o in self.orders
                if o["id"] == oid and o.get("agent_id") == aid
                and o.get("status", "open") == "open"]

    def _r_agent_configs(self, s, a):
        return [{"id": k, "config": v} for k, v in self.agents.items()]

    def _r_traded_flags(self, s, a):
        """core.flag_dormancy: did each past session place an order, newest
        first. Set `traded_sessions` on the fake to drive it."""
        return [{"traded": t} for t in self.traded_sessions]

    def _r_last_reflection(self, s, a):
        return [{"t": self.last_reflection}]

    def _r_kind_filed(self, s, a):
        kind = re.search(r"kind='([a-z_]+)'", s)
        return [{"?column?": 1} for t in self.triggers
                if t["agent_id"] == a[0]
                and (not kind or t["kind"] == kind.group(1))][:1]

    def _r_agent_row(self, s, a):
        aid = a[0]
        if aid not in self.agents:
            return []
        return [{"id": aid, "name": aid, "config": self.agents[aid],
                 "status": "active"}]

    def _w_run(self, s, a):
        rid = 1 + len(self.runs)
        self.runs.append({"id": rid, "agent_id": a[0], "trigger": a[1],
                          "status": "started", "meta": {}})
        return [{"id": rid}] if "returning id" in s else []

    def _w_run_update(self, s, a):
        rid = a[-1]
        status = re.search(r"status='([a-z]+)'", s)
        for r in self.runs:
            if r["id"] != rid:
                continue
            if status:
                r["status"] = status.group(1)
            if "meta" in s:
                patch = a[-2] if len(a) > 1 else "{}"
                meta = dict(r["meta"])
                # `meta - 'journal'`: the entry is in the record now
                for dropped in re.findall(r"meta - '([a-z_]+)'", s):
                    meta.pop(dropped, None)
                r["meta"] = {**meta,
                             **(json.loads(patch) if isinstance(patch, str) else patch)}
        return []

    def _w_handle_triggers(self, s, a):
        for t in self.triggers:
            if t["agent_id"] == a[0]:
                t["handled"] = True
        return []

    def _r_guidance(self, s, a):
        aid, cid = a[0], a[1]
        return [{"id": g["id"]} for g in self.guidance
                if g["agent_id"] == aid and g["cid"] == cid
                and g.get("disposition") is None]

    def _r_guidance_cids(self, s, a):
        return [{"cid": g["cid"]} for g in self.guidance
                if g["agent_id"] == a[0] and g.get("disposition") is None]

    def _r_last_trigger(self, s, a):
        aid = a[0]
        rows = sorted(
            [t for t in self.triggers if t["agent_id"] == aid and t["kind"] == "drawdown"],
            key=lambda t: t["ts"], reverse=True)
        return rows[:1]

    def _r_recovered(self, s, a):
        aid, ts, level = a[0], a[1], float(a[2])
        return [{"?column?": 1} for m in self.marks
                if m["agent_id"] == aid and m["ts"] > ts and m["equity"] >= level][:1]

    # writes ------------------------------------------------------------

    def _w_noop(self, s, a):
        return []

    def _w_order(self, s, a):
        oid = self._next_order_id
        self._next_order_id += 1
        self.orders.append({"id": oid, "agent_id": a[0], "raw": a})
        return [{"id": oid}] if "returning id" in s else []

    def _w_position(self, s, a):
        # (agent_id, symbol, qty, avg_fill, ...)
        sym, qty, fill = a[1], float(a[2]), float(a[3])
        p = self.positions.get(sym)
        if p:
            total = p["qty"] + qty
            p["avg_fill"] = (p["qty"] * p["avg_fill"] + qty * fill) / total
            p["qty"] = total
        else:
            self.positions[sym] = {"qty": qty, "avg_fill": fill}
        return []

    def _w_trigger(self, s, a):
        details = a[1]
        self.triggers.append({
            "agent_id": a[0],
            "kind": re.search(r"values \(%s,'([a-z_]+)'", s).group(1)
            if re.search(r"values \(%s,'([a-z_]+)'", s) else "?",
            "details": json.loads(details) if isinstance(details, str) else details,
            "ts": a[2] if len(a) > 2 else None,
            "handled": "handled" in s,
        })
        return []

    def _w_operation(self, s, a):
        # (run_id, seq, type, payload, verdict, reason)
        self.operations.append((a[2], a[4], a[5]))
        return []

    def _w_mark(self, s, a):
        self.marks.append({"agent_id": a[0], "ts": a[1], "equity": float(a[2])})
        return []

    def _w_peak(self, s, a):
        self.peak_equity = float(a[0])
        return []

    def _w_cash(self, s, a):
        delta = float(a[0])
        self.cash += -delta if "cash-%s" in s or "cash - %s" in s else delta
        return []

    def _w_pos_qty(self, s, a):
        qty, sym = float(a[0]), a[2]
        if "qty=qty-%s" in s:
            self.positions[sym]["qty"] -= qty
        else:
            self.positions[sym]["qty"] = qty
        return []

    def _w_pos_delete(self, s, a):
        self.positions.pop(a[1], None)
        return []

    def _w_order_status(self, s, a):
        status = re.search(r"status='([a-z]+)'", s)
        oid = a[-1]
        for o in self.orders:
            if o["id"] == oid:
                o["status"] = status.group(1) if status else "closed"
                o["reason"] = a[0] if "reason=%s" in s else o.get("reason")
        return []

    def _w_guidance(self, s, a):
        disp, answer, gid = a[0], a[1], a[-1]
        for g in self.guidance:
            if g["id"] == gid:
                g["disposition"], g["answer"] = disp, answer
        return []

    def _w_watchlist(self, s, a):
        self.watchlist[a[0]] = a[2]
        return []


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = Result([])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self._result = self.conn.execute(sql, args)

    def fetchone(self):
        return self._result.fetchone()

    def fetchall(self):
        return self._result.fetchall()


def agent(agent_id="tempo", **config):
    """The `agent` row runner/ops.validate_and_apply is handed."""
    return {"id": agent_id, "config": config}
