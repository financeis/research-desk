"""In-memory stand-ins for the flows tests: the supabase-py builder over the four flows tables and
the two DB functions of migration 009, the KIS client, and the prices window.

``FakeSupabase`` serves the calls features.flows makes: ``table(name)`` with select (columns as
comma-separated text or '*'; ``count='exact'`` and ``head=True``), eq, in_, lt, lte, gte, is_,
``not_`` (negates the next filter), order, limit, range, insert, upsert (``on_conflict`` = the
primary key), update, delete (``returning`` 'representation' or 'minimal'), execute; and
``rpc(name, params)`` for ``rank_investor_flows`` and ``investor_flow_coverage``, whose answer can
be ordered and paged like a table read. It behaves like Supabase / PostgREST where the feature
depends on it:

- one answer holds at most ``MAX_ROWS`` (1000) rows, an RPC answer too; ``count='exact'`` counts
  every matching row and ``head=True`` returns none;
- ``range()`` may be called once per query or RPC: postgrest-py 2.29 appends offset/limit instead
  of replacing them, so a paged read must build a new query for each page;
- a NULL value never passes eq / in_ / lt / lte / gte; ``order`` puts NULLs last ascending and
  first descending;
- a bulk upsert writes the union of its rows' keys (a key missing from one row is NULL there,
  supabase-py's ``default_to_null``); an existing row changes only in those columns; a new row gets
  the table defaults for the columns not written (``fetched_at`` / ``updated_at`` / ``started_at``
  now, ``status`` 'running'); one statement may not touch the same key twice; inserting a run
  gives it the next ``run_id``; inserting a key that exists is a unique violation;
- update and delete need a filter (Supabase refuses them without one);
- the NOT NULL and CHECK constraints of migration 009 hold (spec §5.1: the 11 ``net_*`` amounts are
  NOT NULL integers, so a row written without them is refused), unknown columns are refused, dates
  must be ``YYYY-MM-DD`` text; a violation raises ``FakeDBError`` and the statement writes nothing.
  Payloads must be JSON (a ``date`` object or NaN fails, like supabase-py's request);
- ``rank_investor_flows(p_day, p_caps, p_window=250, p_min_days=60)``: for every row of ``p_day``
  whose ``ranked_at`` is NULL and whose stock is in ``p_caps``, writes ``market_cap`` (the caps'
  value), ``window_days`` and the 11 ranks of the reference calculation
  (``logic.window_ranks``; no ranks under ``p_min_days`` days) and ``ranked_at`` now; other rows
  stay as they are; answers the number of rows written;
- ``investor_flow_coverage(p_since)``: per stock, the count, first and last day of its rows from
  ``p_since`` on, in stock-code order.

Each execution is kept in ``executed``; ``writes()`` lists the inserts, upserts, updates, deletes
and ranking calls. ``fail_next(kind)`` makes the next execution of that kind ('select', 'insert',
'upsert', 'update', 'delete', 'rpc') raise FakeDBError (a DB outage).

``FakeKis`` is the KIS client of one run over a fake market (``MARKET``, weekdays); ``FakePrices``
stands in for the prices window (``latest_run`` and ``snapshots``).
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cmp_to_key
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

from research_desk.features.flows import logic

DAILY = "investor_flow_daily"
RUNS = "investor_flow_runs"
DAYS = "investor_flow_days"
STOCKS = "investor_flow_stocks"
RANK_FUNCTION = "rank_investor_flows"
COVERAGE_FUNCTION = "investor_flow_coverage"
MAX_ROWS = 1000

# migration 009 (spec §5.1)
KEYS = ("pe_fund", "foreigner", "institution", "pension", "trust", "fin_invest", "insurance", "bank",
        "other_fin", "other_corp", "individual")
NET = tuple(f"net_{key}" for key in KEYS)
RANK = tuple(f"rank_{key}" for key in KEYS)
COLUMNS = {
    DAILY: ("stock_code", "trade_date", *NET, "market_cap", "window_days", *RANK, "ranked_at", "fetched_at"),
    RUNS: ("run_id", "started_at", "finished_at", "status", "as_of", "stocks_total", "stocks_ok",
           "stocks_failed", "days_ranked", "message"),
    DAYS: ("trade_date", "ranked_at", "stocks_ranked", "run_id"),
    STOCKS: ("stock_code", "listed_from", "updated_at"),
}
PRIMARY_KEY = {DAILY: ("stock_code", "trade_date"), RUNS: ("run_id",), DAYS: ("trade_date",),
               STOCKS: ("stock_code",)}
NOT_NULL = {DAILY: ("stock_code", "trade_date", *NET, "fetched_at"), RUNS: ("run_id", "started_at", "status"),
            DAYS: ("trade_date", "ranked_at", "stocks_ranked"), STOCKS: ("stock_code", "updated_at")}
DATE_COLUMNS = ("trade_date", "as_of", "listed_from")
STATUSES = ("running", "ok", "partial", "failed")

# The DB settings the tests give (never a real project).
URL, KEY = "https://example.supabase.test", "service-key"


class FakeDBError(Exception):
    """What a failed request raises (postgrest's APIError in the real client)."""


def _key(table: str, row: dict) -> tuple:
    return tuple(row.get(name) for name in PRIMARY_KEY[table])


def _is_day(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class _Paging:
    """order / limit / range shared by table reads and RPC answers."""

    def _init_paging(self) -> None:
        self.orders: list[tuple[str, bool]] = []   # (column, desc)
        self.offset: Optional[int] = None
        self.limit_n: Optional[int] = None

    def order(self, column: str, *, desc: bool = False, nullsfirst: Optional[bool] = None):
        assert nullsfirst is None, "the flows reads keep PostgreSQL's NULL placement"
        self.orders.append((column, desc))
        return self

    def limit(self, size: int):
        self.limit_n = size
        return self

    def range(self, start: int, end: int):
        assert self.offset is None, ("range() twice on one query: postgrest-py appends offset/limit "
                                     "instead of replacing them; build a new query for each page")
        self.offset, self.limit_n = start, end - start + 1
        return self

    def _page(self, rows: list[dict]) -> list[dict]:
        def compare(a: dict, b: dict) -> int:
            for column, desc in self.orders:
                x, y = a.get(column), b.get(column)
                if x == y:
                    continue
                if x is None or y is None:   # NULLs last ascending, first descending
                    return -1 if (x is None) == desc else 1
                return (1 if x > y else -1) * (-1 if desc else 1)
            return 0
        rows = sorted(rows, key=cmp_to_key(compare))
        if self.offset:
            rows = rows[self.offset:]
        if self.limit_n is not None:
            rows = rows[:self.limit_n]
        return rows[:MAX_ROWS]


class FakeQuery(_Paging):
    """One chained query on one of the four tables."""

    def __init__(self, db: "FakeSupabase", table: str) -> None:
        assert table in COLUMNS, f"flows reads and writes only its four tables, got {table!r}"
        self.db = db
        self.table = table
        self.kind: Optional[str] = None
        self.columns: Optional[list[str]] = None        # None: every column
        self.payload: Any = None
        self.on_conflict: Optional[str] = None
        self.returning = "representation"
        self.count: Optional[str] = None
        self.head = False
        self.filters: list[tuple[str, str, Any, bool]] = []   # (operator, column, value, negated)
        self._negate = False
        self._init_paging()

    # ── building ─────────────────────────────────────────────────────────────

    def _begin(self, kind: str) -> None:
        assert self.kind is None, f"one query is one statement: {self.kind} then {kind}"
        self.kind = kind

    def select(self, *columns: str, count: Optional[str] = None, head: Optional[bool] = None) -> "FakeQuery":
        self._begin("select")
        names = [name.strip() for text in (columns or ("*",)) for name in text.split(",")]
        if names != ["*"]:
            for name in names:
                self.db.check_column(self.table, name)
            self.columns = names
        assert count in (None, "exact"), count
        self.count, self.head = count, bool(head)
        return self

    def insert(self, json_: Any, *, returning: str = "representation", **options: Any) -> "FakeQuery":
        self._begin("insert")
        self.payload, self.returning = self.db.as_json(json_), returning
        return self

    def upsert(self, json_: Any, *, on_conflict: str = "", returning: str = "representation",
               **options: Any) -> "FakeQuery":
        self._begin("upsert")
        self.payload, self.on_conflict, self.returning = self.db.as_json(json_), on_conflict, returning
        return self

    def update(self, json_: dict, *, returning: str = "representation", **options: Any) -> "FakeQuery":
        self._begin("update")
        self.payload, self.returning = self.db.as_json(json_), returning
        return self

    def delete(self, *, returning: str = "representation", **options: Any) -> "FakeQuery":
        self._begin("delete")
        self.returning = returning
        return self

    @property
    def not_(self) -> "FakeQuery":
        self._negate = True
        return self

    def _filter(self, operator: str, column: str, value: Any) -> "FakeQuery":
        self.db.check_column(self.table, column)
        self.filters.append((operator, column, value, self._negate))
        self._negate = False
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        return self._filter("eq", column, value)

    def in_(self, column: str, values: Iterable[Any]) -> "FakeQuery":
        return self._filter("in", column, list(values))

    def lt(self, column: str, value: Any) -> "FakeQuery":
        return self._filter("lt", column, value)

    def lte(self, column: str, value: Any) -> "FakeQuery":
        return self._filter("lte", column, value)

    def gte(self, column: str, value: Any) -> "FakeQuery":
        return self._filter("gte", column, value)

    def is_(self, column: str, value: Any) -> "FakeQuery":
        assert value in ("null", None), "the flows reads only test for NULL"
        return self._filter("is", column, None)

    def order(self, column: str, *, desc: bool = False, nullsfirst: Optional[bool] = None) -> "FakeQuery":
        self.db.check_column(self.table, column)
        return super().order(column, desc=desc, nullsfirst=nullsfirst)

    # ── looking at it ────────────────────────────────────────────────────────

    def filter_values(self, operator: str, column: str) -> list[Any]:
        return [value for op, col, value, _ in self.filters if (op, col) == (operator, column)]

    @property
    def rows_sent(self) -> list[dict]:
        """The rows of an insert or upsert."""
        return self.payload if isinstance(self.payload, list) else [self.payload]

    # ── running it ───────────────────────────────────────────────────────────

    def execute(self) -> SimpleNamespace:
        assert self.kind is not None, "a query must call select / insert / upsert / update / delete first"
        assert not self._negate, "not_ without a filter after it"
        self.db.executed.append(self)
        if self.db.failing.get(self.kind):
            self.db.failing[self.kind] -= 1
            raise FakeDBError(f"fake DB outage on {self.kind} {self.table}")
        return getattr(self, f"_{self.kind}")()

    def _passes(self, row: dict) -> bool:
        for operator, column, value, negated in self.filters:
            cell = row.get(column)
            if operator == "is":
                passed = cell is None
            elif cell is None:
                passed = False
            elif operator == "eq":
                passed = cell == value
            elif operator == "in":
                passed = cell in value
            elif operator == "lt":
                passed = cell < value
            elif operator == "lte":
                passed = cell <= value
            else:   # gte
                passed = cell >= value
            if passed == negated:
                return False
        return True

    def _matched(self) -> list[dict]:
        return [row for row in self.db.tables[self.table].values() if self._passes(row)]

    def _answer(self, rows: list[dict], count: Optional[int] = None) -> SimpleNamespace:
        if self.kind != "select" and self.returning == "minimal":
            return SimpleNamespace(data=[], count=count)
        return SimpleNamespace(data=deepcopy(rows), count=count)

    def _select(self) -> SimpleNamespace:
        matched = self._matched()
        count = len(matched) if self.count else None
        if self.head:
            return SimpleNamespace(data=[], count=count)
        rows = self._page(matched)
        if self.columns is not None:
            rows = [{name: row.get(name) for name in self.columns} for row in rows]
        return SimpleNamespace(data=deepcopy(rows), count=count)

    def _new_row(self, values: dict) -> dict:
        return {name: None for name in COLUMNS[self.table]} | self.db.defaults(self.table) | values

    def _insert(self) -> SimpleNamespace:
        table = self.db.tables[self.table]
        made = []
        next_id = self.db.next_run_id
        for sent in self.rows_sent:
            for name in sent:
                self.db.check_column(self.table, name)
            row = self._new_row(deepcopy(sent))
            if self.table == RUNS and row["run_id"] is None:
                row["run_id"] = next_id
                next_id += 1
            self.db.check_row(self.table, row)
            if _key(self.table, row) in table or any(_key(self.table, other) == _key(self.table, row)
                                                     for other in made):
                raise FakeDBError(f'duplicate key value violates unique constraint "{self.table}_pkey"')
            made.append(row)
        for row in made:
            table[_key(self.table, row)] = row
            if self.table == RUNS:
                self.db.next_run_id = max(self.db.next_run_id, row["run_id"] + 1)
        return self._answer(made)

    def _upsert(self) -> SimpleNamespace:
        assert self.table in (DAILY, STOCKS), "flows upserts raw rows and data starts only"
        assert self.on_conflict == ",".join(PRIMARY_KEY[self.table]), (
            f"the upsert must resolve on the primary key, got {self.on_conflict!r}")
        sent = self.rows_sent
        columns = list(dict.fromkeys(name for row in sent for name in row))   # PostgREST `columns`
        for name in columns:
            self.db.check_column(self.table, name)
        keys = [_key(self.table, row) for row in sent]
        if len(set(keys)) != len(keys):
            raise FakeDBError("ON CONFLICT DO UPDATE command cannot affect row a second time")
        table = self.db.tables[self.table]
        written = []
        for key, row in zip(keys, sent):
            values = {name: deepcopy(row.get(name)) for name in columns}
            written.append((key, table[key] | values if key in table else self._new_row(values)))
        for _, row in written:
            self.db.check_row(self.table, row)
        for key, row in written:
            table[key] = row
        return self._answer([row for _, row in written])

    def _update(self) -> SimpleNamespace:
        assert self.filters, "Supabase refuses an UPDATE without a filter"
        for name in self.payload:
            self.db.check_column(self.table, name)
        matched = self._matched()
        for row in matched:
            self.db.check_row(self.table, row | self.payload)
        for row in matched:
            row.update(deepcopy(self.payload))
        return self._answer(matched)

    def _delete(self) -> SimpleNamespace:
        assert self.filters, "Supabase refuses a DELETE without a filter"
        matched = self._matched()
        table = self.db.tables[self.table]
        for row in matched:
            del table[_key(self.table, row)]
        return self._answer(matched)


class FakeRPC(_Paging):
    """One call of a DB function; a table function's answer can be ordered and paged."""

    kind = "rpc"

    def __init__(self, db: "FakeSupabase", name: str, params: dict) -> None:
        self.db, self.name = db, name
        self.params = db.as_json(params)
        self._init_paging()

    @property
    def table(self) -> str:
        return self.name

    def execute(self) -> SimpleNamespace:
        self.db.executed.append(self)
        if self.db.failing.get("rpc"):
            self.db.failing["rpc"] -= 1
            raise FakeDBError(f"fake DB outage on rpc {self.name}")
        if self.name == RANK_FUNCTION:
            assert not self.orders and self.offset is None and self.limit_n is None
            return SimpleNamespace(data=self.db.rank(self.params), count=None)
        if self.name == COVERAGE_FUNCTION:
            return SimpleNamespace(data=deepcopy(self._page(self.db.coverage(self.params))), count=None)
        raise FakeDBError(f"Could not find the function public.{self.name}")


class FakeSupabase:
    """``table(name)`` starts a FakeQuery over ``tables[name]`` (primary key → row);
    ``rpc(name, params)`` calls a DB function."""

    def __init__(self, now: str = "2026-10-09T10:00:00+00:00", **rows: list[dict]) -> None:
        self.tables: dict[str, dict[tuple, dict]] = {name: {} for name in COLUMNS}
        self.now = now   # the DB's now() for column defaults and ranked_at
        self.next_run_id = 1
        self.executed: list[Any] = []
        self.failing: dict[str, int] = {}
        self.made: list[tuple[str, str]] = []   # (url, key) of each client handed out
        for table, table_rows in rows.items():
            for row in table_rows:
                self.add(table, row)

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def rpc(self, name: str, params: dict, count: Any = None, head: bool = False, get: bool = False) -> FakeRPC:
        return FakeRPC(self, name, params)

    # ── setting up ───────────────────────────────────────────────────────────

    def defaults(self, table: str) -> dict:
        return {DAILY: {"fetched_at": self.now}, RUNS: {"started_at": self.now, "status": "running"},
                DAYS: {}, STOCKS: {"updated_at": self.now}}[table]

    def add(self, table: str, row: dict) -> dict:
        """Store ``row`` as it is (the table defaults for the columns not given)."""
        full = {name: None for name in COLUMNS[table]} | self.defaults(table) | deepcopy(row)
        if table == RUNS:
            if full["run_id"] is None:
                full["run_id"] = self.next_run_id
            self.next_run_id = max(self.next_run_id, full["run_id"] + 1)
        for name in row:
            self.check_column(table, name)
        self.check_row(table, full)
        self.tables[table][_key(table, full)] = full
        return full

    def add_raw(self, code: str, day: date, **values: Any) -> dict:
        """A stored raw row of ``code`` on ``day``: every amount 0 unless given."""
        return self.add(DAILY, {"stock_code": code, "trade_date": day.isoformat(),
                                **{name: 0 for name in NET}, **values})

    def fail_next(self, kind: str, times: int = 1) -> None:
        self.failing[kind] = self.failing.get(kind, 0) + times

    # ── looking at it ────────────────────────────────────────────────────────

    def rows(self, table: str, **where: Any) -> list[dict]:
        """Rows of ``table`` whose columns equal ``where`` (copies), in key order."""
        found = [row for key, row in sorted(self.tables[table].items(), key=lambda item: _sortable(item[0]))
                 if all(row.get(name) == value for name, value in where.items())]
        return deepcopy(found)

    def row(self, table: str, *key: Any) -> Optional[dict]:
        found = self.tables[table].get(tuple(key))
        return deepcopy(found) if found is not None else None

    def runs(self) -> list[dict]:
        return self.rows(RUNS)

    def raw_days(self, code: str) -> list[str]:
        """The stored trading days of ``code``, oldest first."""
        return [row["trade_date"] for row in self.rows(DAILY, stock_code=code)]

    def queries(self, table: str, kind: Optional[str] = None) -> list[Any]:
        return [q for q in self.executed if q.table == table and kind in (None, q.kind)]

    def rpcs(self, name: str) -> list[FakeRPC]:
        return [q for q in self.executed if q.kind == "rpc" and q.name == name]

    def writes(self) -> list[Any]:
        return [q for q in self.executed
                if q.kind in ("insert", "upsert", "update", "delete")
                or (q.kind == "rpc" and q.name == RANK_FUNCTION)]

    # ── the DB functions ─────────────────────────────────────────────────────

    def rank(self, params: dict) -> int:
        unknown = set(params) - {"p_day", "p_caps", "p_window", "p_min_days"}
        if unknown or "p_day" not in params:
            raise FakeDBError(f"Could not find the function public.{RANK_FUNCTION}({sorted(params)})")
        day = params["p_day"]
        caps = params.get("p_caps", {})
        window, min_days = params.get("p_window", 250), params.get("p_min_days", 60)
        if not _is_day(day):
            raise FakeDBError(f'invalid input syntax for type date: "{day}"')
        if not (isinstance(caps, dict) and all(isinstance(code, str) and isinstance(cap, int)
                                               for code, cap in caps.items())):
            raise FakeDBError("p_caps must be a JSON object of stock code to whole won")
        if not (isinstance(window, int) and isinstance(min_days, int)):
            raise FakeDBError("p_window and p_min_days are integers")
        table = self.tables[DAILY]
        by_stock: dict[str, list[dict]] = {}
        for row in table.values():
            by_stock.setdefault(row["stock_code"], []).append(row)
        changed = 0
        for row in table.values():
            code = row["stock_code"]
            if row["trade_date"] != day or row["ranked_at"] is not None or code not in caps:
                continue
            count, ranks = logic.window_ranks(by_stock[code], day, window, min_days)
            row.update({"market_cap": caps[code], "window_days": count, "ranked_at": self.now})
            row.update({f"rank_{key}": (ranks[key] if ranks else None) for key in KEYS})
            changed += 1
        return changed

    def coverage(self, params: dict) -> list[dict]:
        if set(params) != {"p_since"} or not _is_day(params["p_since"]):
            raise FakeDBError(f"Could not find the function public.{COVERAGE_FUNCTION}({sorted(params)})")
        since = params["p_since"]
        days: dict[str, list[str]] = {}
        for row in self.tables[DAILY].values():
            if row["trade_date"] >= since:
                days.setdefault(row["stock_code"], []).append(row["trade_date"])
        return [{"stock_code": code, "days": len(found), "first_day": min(found), "last_day": max(found)}
                for code, found in sorted(days.items())]

    # ── the DB's rules ───────────────────────────────────────────────────────

    @staticmethod
    def as_json(payload: Any) -> Any:
        """The payload as the request body carries it: JSON only, no NaN or infinity."""
        try:
            return json.loads(json.dumps(payload, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"not a JSON request body: {exc}") from exc

    @staticmethod
    def check_column(table: str, name: str) -> None:
        if name not in COLUMNS[table]:
            raise FakeDBError(f'column "{name}" of relation "{table}" does not exist')

    @staticmethod
    def check_row(table: str, row: dict) -> None:
        for name in NOT_NULL[table]:
            if row.get(name) is None:
                raise FakeDBError(f'null value in column "{name}" of relation "{table}" violates not-null constraint')
        for name in DATE_COLUMNS:
            if name in row and row[name] is not None and not _is_day(row[name]):
                raise FakeDBError(f'invalid input syntax for type date: "{row[name]}"')
        if table == RUNS and row["status"] not in STATUSES:
            raise FakeDBError('new row violates check constraint "chk_investor_flow_runs_status"')
        if table == DAILY:
            for name in NET:
                if not (isinstance(row[name], int) and -2**31 <= row[name] < 2**31):
                    raise FakeDBError(f'"{name}" must be an integer, got {row[name]!r}')
            for name in (*RANK, "window_days"):
                if row[name] is not None and not (isinstance(row[name], int) and 0 < row[name] < 2**15):
                    raise FakeDBError(f'"{name}" must be a smallint, got {row[name]!r}')
            if row["market_cap"] is not None and not isinstance(row["market_cap"], int):
                raise FakeDBError(f'"market_cap" must be a bigint, got {row["market_cap"]!r}')
        if table == DAYS and not isinstance(row["stocks_ranked"], int):
            raise FakeDBError('"stocks_ranked" must be an integer')


def _sortable(key: tuple) -> tuple:
    return tuple((value is None, value) for value in key)


# ── KIS ──────────────────────────────────────────────────────────────────────

BLOCK = 30   # trading days in one KIS answer
CLOSE = 61_000
# Every amount field of the KIS answer (KisClient.investor_daily keeps the fields ending in pbmn);
# the last two belong to no investor group.
FIELDS = ("pe_fund_ntby_tr_pbmn", "frgn_reg_ntby_pbmn", "orgn_ntby_tr_pbmn", "etc_orgt_ntby_tr_pbmn",
          "fund_ntby_tr_pbmn", "ivtr_ntby_tr_pbmn", "scrt_ntby_tr_pbmn", "insu_ntby_tr_pbmn",
          "bank_ntby_tr_pbmn", "mrbn_ntby_tr_pbmn", "etc_corp_ntby_tr_pbmn", "prsn_ntby_tr_pbmn",
          "frgn_nreg_ntby_pbmn", "frgn_ntby_tr_pbmn")


def weekdays(start: date, end: date) -> list[date]:
    """The weekdays from ``start`` to ``end``, both included."""
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


MARKET = weekdays(date(2024, 7, 1), date(2026, 12, 31))   # the fake market's trading days


def amounts(code: str, day: date) -> dict[str, int]:
    """The KIS amount fields of ``code`` on ``day`` (millions of won): fixed, varied, signed."""
    seed = sum(map(ord, code)) * 7919 + day.toordinal()
    return {name: (seed * (i + 3) * 2_654_435_761) % 4001 - 2000 for i, name in enumerate(FIELDS)}


@dataclass
class Listing:
    listed: Optional[date] = None          # days before it come as zero-close padding
    history_from: Optional[date] = None    # KIS has no row before it
    missing: frozenset = field(default_factory=frozenset)   # days KIS never returns


class FakeKis:
    """The KIS client of one run: ``investor_daily(code, anchor)`` answers the ``BLOCK`` market days
    on or before the anchor, oldest first, each ``{"date", "close", <FIELDS>}``, with the code's
    ``Listing`` applied and ``values[(code, day)]`` laid over a row. ``fail(code, error, call=n)``
    makes that code's n-th call (1 = its first, the latest block) raise, every call without ``n``.
    Every call is kept in ``calls``; ``during(call)`` runs inside each call (to interrupt a run).
    ``ensure_token()`` is counted in ``token_asked``, not in ``calls``: it runs ``token_during()``,
    then raises ``token_error`` when one is set. ``closed`` after ``close()``."""

    def __init__(self, market: Iterable[date] = MARKET) -> None:
        self.market = sorted(market)
        self.listings: dict[str, Listing] = {}
        self.values: dict[tuple[str, date], dict] = {}
        self.errors: dict[tuple[str, Optional[int]], BaseException] = {}
        self.calls: list[tuple] = []
        self.during: Optional[Callable[[tuple], None]] = None
        self.token_asked = 0
        self.token_error: Optional[BaseException] = None
        self.token_during: Optional[Callable[[], None]] = None
        self.closed = False
        self.history_from: Optional[date] = None   # every code's history starts here unless listed

    def listing(self, code: str, *, listed: Optional[date] = None, history_from: Optional[date] = None,
                missing: Iterable[date] = ()) -> None:
        self.listings[code] = Listing(listed, history_from, frozenset(missing))

    def fail(self, code: str, error: BaseException, call: Optional[int] = None) -> None:
        self.errors[(code, call)] = error

    def ensure_token(self) -> None:
        assert not self.closed, "the KIS client was used after close()"
        self.token_asked += 1
        if self.token_during is not None:
            self.token_during()
        if self.token_error is not None:
            raise self.token_error

    def investor_daily(self, code: str, anchor: date) -> list[dict]:
        assert not self.closed, "the KIS client was used after close()"
        assert isinstance(anchor, date), anchor
        call = ("investor_daily", code, anchor)
        self.calls.append(call)
        if self.during is not None:
            self.during(call)
        number = len(self.anchors(code))
        error = self.errors.get((code, number)) or self.errors.get((code, None))
        if error is not None:
            raise error
        listing = self.listings.get(code, Listing())
        start = listing.history_from or self.history_from
        rows = []
        for day in [day for day in self.market if day <= anchor][-BLOCK:]:
            if (start is not None and day < start) or day in listing.missing:
                continue
            padded = listing.listed is not None and day < listing.listed
            row = {"date": day, "close": 0 if padded else CLOSE,
                   **({name: 0 for name in FIELDS} if padded else amounts(code, day))}
            row.update(self.values.get((code, day), {}))
            rows.append(row)
        return rows

    def codes(self) -> list[str]:
        """The codes asked, in order, each once."""
        return list(dict.fromkeys(call[1] for call in self.calls))

    def anchors(self, code: str) -> list[date]:
        """The anchors asked for ``code``, in order."""
        return [call[2] for call in self.calls if call[1] == code]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeKis":
        self.closed = False   # a test may run the command twice on one stand-in
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ── the prices window ────────────────────────────────────────────────────────

class FakePrices:
    """The prices window's ``latest_run()`` (``run``) and ``snapshots(codes)`` (``prices``, by
    code); every call is kept in ``calls``."""

    def __init__(self) -> None:
        self.run: Optional[dict] = None
        self.prices: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def latest_run(self) -> Optional[dict]:
        self.calls.append(("latest_run",))
        return deepcopy(self.run)

    def snapshots(self, codes: Iterable[str]) -> dict[str, dict]:
        assert not isinstance(codes, str), "one plain string is a TypeError in the real window"
        wanted = list(dict.fromkeys(codes))
        self.calls.append(("snapshots", wanted))
        return {code: deepcopy(self.prices[code]) for code in wanted if code in self.prices}

    def snapshot(self, code: str, as_of: Optional[str], market_cap: Optional[int]) -> None:
        """A stored price of ``code`` with that as_of and market cap (the window's shape)."""
        self.prices[code] = {"market": "KOSPI", "as_of": as_of, "close": CLOSE, "market_cap": market_cap,
                             "avg_value_20d": 1_000_000, "traded": True,
                             "returns": {"1w": 1.0, "1m": 2.0, "3m": 3.0},
                             "excess": {"1w": 0.0, "1m": 0.0, "3m": 0.0}, "flags": []}
