"""In-memory stand-ins for the prices tests: the supabase-py builder over the three price tables,
and the KIS client.

``FakeSupabase`` serves the calls features.prices makes: select (columns as one comma-separated
string, or '*'), eq, in_, gte, lt, order, limit, range, insert, upsert (on_conflict='stock_code'
for snapshots, 'stock_code,trade_date' for the history), update, delete, execute. It behaves like
Supabase / PostgREST where the feature depends on it:

- one response holds at most ``MAX_ROWS`` (1000) rows;
- a NULL value never passes eq / in_ / gte / lt; ``order`` puts NULLs last ascending and first
  descending;
- a bulk upsert writes the union of its rows' keys (a key missing from one row is NULL there,
  supabase-py's ``default_to_null``); an existing row changes only in those columns; a new row
  gets the table defaults for the columns not written (snapshots: flags '{}', source 'KIS',
  updated_at now); one statement may not touch the same row (the same conflict key) twice;
- inserting a run gives it the next ``run_id`` and the defaults (started_at now, status
  'running');
- the NOT NULL and CHECK constraints of migrations 008 / 009 hold and unknown columns are refused;
  a history close must be a whole number (bigint) and its trade_date a day; a violation raises
  ``FakeDBError`` and the statement writes nothing (like the API error of a real request).
  Payloads must be JSON (a ``date`` object or NaN fails, like supabase-py's request);
- a delete needs a filter (only the history is deleted from);
- ``returning='minimal'`` sends nothing back (``data`` is ``[]``), as ``Prefer: return=minimal``;
- ``range()`` may be called once per query: postgrest-py 2.29 appends offset/limit instead of
  replacing them, so a paged read must build a new query for each page.

Each execution is kept in ``executed``; ``writes()`` lists the inserts, upserts, updates and
deletes. ``fail_next(kind, table=None)`` makes the next query of that kind ('select', 'insert',
'upsert', 'update', 'delete'), on that table when one is named, raise FakeDBError (a DB outage).

``FakeKis`` is the KIS client of one run (``KisClient.ensure_token`` / ``daily_prices`` / ``quote``).
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, timedelta
from functools import cmp_to_key
from types import SimpleNamespace
from typing import Any, Callable, Optional

SNAPSHOT = "stock_price_snapshot"
RUNS = "price_update_runs"
DAILY = "stock_price_daily"
MAX_ROWS = 1000

# migration 008
SNAPSHOT_COLUMNS = ("stock_code", "market", "as_of", "close", "market_cap", "avg_value_20d", "traded",
                    "ret_1w", "ret_1m", "ret_3m", "ret_6m", "xret_1w", "xret_1m", "xret_3m", "xret_6m",
                    "flags", "source", "updated_at")
RUN_COLUMNS = ("run_id", "started_at", "finished_at", "status", "as_of", "stocks_total", "stocks_ok",
               "stocks_failed", "message")
# migration 009: the adjusted close (수정 종가, won) of a stock on a day, primary key (stock_code, trade_date)
DAILY_COLUMNS = ("stock_code", "trade_date", "close")
COLUMNS = {SNAPSHOT: SNAPSHOT_COLUMNS, RUNS: RUN_COLUMNS, DAILY: DAILY_COLUMNS}
# The conflict key of each table prices upserts into (its primary key).
UPSERT_KEYS = {SNAPSHOT: ("stock_code",), DAILY: ("stock_code", "trade_date")}
MARKETS = (None, "KOSPI", "KOSDAQ")
STATUSES = ("running", "ok", "partial", "failed")
BIGINT = (-2**63, 2**63 - 1)

# The DB settings the tests give (never a real project).
URL, KEY = "https://example.supabase.test", "service-key"


class FakeDBError(Exception):
    """What a failed request raises (postgrest's APIError in the real client)."""


class FakeQuery:
    """One chained query on one of the three tables."""

    def __init__(self, db: "FakeSupabase", table: str) -> None:
        assert table in COLUMNS, f"prices reads and writes only its three tables, got {table!r}"
        self.db = db
        self.table = table
        self.kind: Optional[str] = None
        self.columns: Optional[list[str]] = None       # None: every column
        self.payload: Any = None
        self.on_conflict: Optional[str] = None
        self.returning = "representation"              # 'minimal': nothing sent back
        self.filters: list[tuple[str, str, Any]] = []  # (operator, column, value)
        self.orders: list[tuple[str, bool]] = []       # (column, desc)
        self.offset: Optional[int] = None
        self.limit_n: Optional[int] = None

    # ── building ─────────────────────────────────────────────────────────────

    def _begin(self, kind: str, options: Optional[dict] = None) -> None:
        assert self.kind is None, f"one query is one statement: {self.kind} then {kind}"
        self.kind = kind
        returning = str((options or {}).get("returning", "representation"))
        assert returning in ("representation", "minimal"), f"unknown returning {returning!r}"
        self.returning = returning

    def select(self, columns: str = "*", count: Optional[str] = None) -> "FakeQuery":
        self._begin("select")
        if columns.strip() != "*":
            self.columns = [name.strip() for name in columns.split(",")]
            for name in self.columns:
                self.db.check_column(self.table, name)
        return self

    def insert(self, payload: Any, **options: Any) -> "FakeQuery":
        self._begin("insert", options)
        self.payload = self.db.as_json(payload)
        return self

    def upsert(self, payload: Any, *, on_conflict: str = "", **options: Any) -> "FakeQuery":
        self._begin("upsert", options)
        self.payload, self.on_conflict = self.db.as_json(payload), on_conflict
        return self

    def update(self, values: dict, **options: Any) -> "FakeQuery":
        self._begin("update", options)
        self.payload = self.db.as_json(values)
        return self

    def delete(self, **options: Any) -> "FakeQuery":
        self._begin("delete", options)
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self.db.check_column(self.table, column)
        self.filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values) -> "FakeQuery":
        self.db.check_column(self.table, column)
        self.filters.append(("in", column, list(values)))
        return self

    def gte(self, column: str, value: Any) -> "FakeQuery":
        self.db.check_column(self.table, column)
        self.filters.append(("gte", column, value))
        return self

    def lt(self, column: str, value: Any) -> "FakeQuery":
        self.db.check_column(self.table, column)
        self.filters.append(("lt", column, value))
        return self

    def order(self, column: str, *, desc: bool = False, nullsfirst: Optional[bool] = None) -> "FakeQuery":
        self.db.check_column(self.table, column)
        assert nullsfirst is None, "the prices reads keep PostgreSQL's NULL placement"
        self.orders.append((column, desc))
        return self

    def limit(self, size: int) -> "FakeQuery":
        self.limit_n = size
        return self

    def range(self, start: int, end: int) -> "FakeQuery":
        assert self.offset is None, ("range() twice on one query: postgrest-py appends offset/limit "
                                     "instead of replacing them; build a new query for each page")
        self.offset, self.limit_n = start, end - start + 1
        return self

    # ── looking at it ────────────────────────────────────────────────────────

    def filter_values(self, operator: str, column: str) -> list[Any]:
        return [value for op, col, value in self.filters if (op, col) == (operator, column)]

    @property
    def rows_sent(self) -> list[dict]:
        """The rows of an insert or upsert."""
        return self.payload if isinstance(self.payload, list) else [self.payload]

    # ── running it ───────────────────────────────────────────────────────────

    def execute(self) -> SimpleNamespace:
        assert self.kind is not None, "a query must call select / insert / upsert / update / delete first"
        self.db.executed.append(self)
        for target in ((self.kind, self.table), (self.kind, None)):
            if self.db.failing.get(target):
                self.db.failing[target] -= 1
                raise FakeDBError(f"fake DB outage on {self.kind} {self.table}")
        data = getattr(self, f"_{self.kind}")()
        return SimpleNamespace(data=[] if self.returning == "minimal" else data, count=None)

    def _passes(self, row: dict) -> bool:
        for operator, column, value in self.filters:
            cell = row.get(column)
            if cell is None:
                return False
            if operator == "eq" and cell != value:
                return False
            if operator == "in" and cell not in value:
                return False
            if operator == "gte" and not cell >= value:
                return False
            if operator == "lt" and not cell < value:
                return False
        return True

    def _sorted(self, rows: list[dict]) -> list[dict]:
        def compare(a: dict, b: dict) -> int:
            for column, desc in self.orders:
                x, y = a.get(column), b.get(column)
                if x == y:
                    continue
                if x is None or y is None:   # NULLs last ascending, first descending
                    return -1 if (x is None) == desc else 1
                return (1 if x > y else -1) * (-1 if desc else 1)
            return 0
        return sorted(rows, key=cmp_to_key(compare))

    def _select(self) -> list[dict]:
        rows = self._sorted([row for row in self.db.tables[self.table] if self._passes(row)])
        if self.offset:
            rows = rows[self.offset:]
        if self.limit_n is not None:
            rows = rows[:self.limit_n]
        rows = rows[:MAX_ROWS]
        if self.columns is None:
            return deepcopy(rows)
        return [{name: deepcopy(row.get(name)) for name in self.columns} for row in rows]

    def _insert(self) -> list[dict]:
        assert self.table == RUNS, "prices inserts only run records"
        made = []
        for sent in self.rows_sent:
            for name in sent:
                self.db.check_column(RUNS, name)
            row = {name: None for name in RUN_COLUMNS} | {"started_at": self.db.now, "status": "running"}
            row |= deepcopy(sent)
            if row["run_id"] is None:
                row["run_id"] = self.db.next_run_id + len(made)
            self.db.check_run(row)
            made.append(row)
        self.db.next_run_id += len(made)
        self.db.tables[RUNS].extend(made)
        return deepcopy(made)

    def _upsert(self) -> list[dict]:
        key = UPSERT_KEYS.get(self.table)
        assert key is not None and self.on_conflict == ",".join(key), (
            "prices upserts snapshot rows on stock_code and history rows on stock_code,trade_date, "
            f"got {self.table} on {self.on_conflict!r}")
        sent = self.rows_sent
        columns = list(dict.fromkeys(name for row in sent for name in row))   # PostgREST `columns`
        for name in columns:
            self.db.check_column(self.table, name)
        keys = [tuple(row.get(name) for name in key) for row in sent]
        if len(set(keys)) != len(keys):
            raise FakeDBError("ON CONFLICT DO UPDATE command cannot affect row a second time")
        table = {tuple(row[name] for name in key): deepcopy(row) for row in self.db.tables[self.table]}
        order = list(table)
        written = []
        for row, row_key in zip(sent, keys):
            values = {name: deepcopy(row.get(name)) for name in columns}
            if row_key in table:
                table[row_key].update(values)
            else:
                table[row_key] = self.db.new_row(self.table) | values
                order.append(row_key)
            written.append(table[row_key])
        check = self.db.checker(self.table)
        for row in written:
            check(row)
        self.db.tables[self.table] = [table[row_key] for row_key in order]
        return deepcopy(written)

    def _update(self) -> list[dict]:
        for name in self.payload:
            self.db.check_column(self.table, name)
        matched = [row for row in self.db.tables[self.table] if self._passes(row)]
        check = self.db.checker(self.table)
        for row in matched:
            check(row | self.payload)
        for row in matched:
            row.update(deepcopy(self.payload))
        return deepcopy(matched)

    def _delete(self) -> list[dict]:
        assert self.table == DAILY, "prices deletes only history rows"
        assert self.filters, "a delete without a filter would empty the table"
        kept, deleted = [], []
        for row in self.db.tables[self.table]:
            (deleted if self._passes(row) else kept).append(row)
        self.db.tables[self.table] = kept
        return deepcopy(deleted)


class FakeSupabase:
    """``table(name)`` starts a FakeQuery over ``tables[name]`` (a list of row dicts)."""

    def __init__(self, snapshots: Optional[list[dict]] = None, runs: Optional[list[dict]] = None,
                 now: str = "2026-10-01T00:00:00+00:00", daily: Optional[list[dict]] = None) -> None:
        self.tables: dict[str, list[dict]] = {SNAPSHOT: [], RUNS: [], DAILY: []}
        self.now = now   # the DB's now() for column defaults
        self.next_run_id = 1
        self.executed: list[FakeQuery] = []
        self.failing: dict[tuple[str, Optional[str]], int] = {}
        self.made: list[tuple[str, str]] = []   # (url, key) of each client handed out
        for row in snapshots or []:
            self.add_snapshot(row)
        for row in runs or []:
            self.add_run(row)
        for row in daily or []:
            self.add_daily(row)

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    # ── setting up ───────────────────────────────────────────────────────────

    def add_snapshot(self, row: dict) -> dict:
        full = self.new_row(SNAPSHOT) | deepcopy(row)
        self.check_snapshot(full)
        self.tables[SNAPSHOT].append(full)
        return full

    def add_run(self, row: dict) -> dict:
        full = {name: None for name in RUN_COLUMNS} | {"status": "running", "started_at": self.now} | deepcopy(row)
        if full["run_id"] is None:
            full["run_id"] = self.next_run_id
        self.next_run_id = max(self.next_run_id, full["run_id"] + 1)
        self.check_run(full)
        self.tables[RUNS].append(full)
        return full

    def add_daily(self, row: dict) -> dict:
        """Store one history row ``{stock_code, trade_date: 'YYYY-MM-DD', close}``."""
        full = self.new_row(DAILY) | deepcopy(row)
        self.check_daily(full)
        assert all((other["stock_code"], other["trade_date"]) != (full["stock_code"], full["trade_date"])
                   for other in self.tables[DAILY]), "one history row per stock and day"
        self.tables[DAILY].append(full)
        return full

    def fail_next(self, kind: str, times: int = 1, table: Optional[str] = None) -> None:
        self.failing[(kind, table)] = self.failing.get((kind, table), 0) + times

    # ── looking at it ────────────────────────────────────────────────────────

    def snapshot(self, code: str) -> Optional[dict]:
        found = [row for row in self.tables[SNAPSHOT] if row["stock_code"] == code]
        return deepcopy(found[0]) if found else None

    def runs(self) -> list[dict]:
        return deepcopy(self.tables[RUNS])

    def history(self) -> dict[str, dict[str, int]]:
        """The stored history as ``{stock_code: {trade_date: close}}``, codes and days in order."""
        found: dict[str, dict[str, int]] = {}
        for row in sorted(self.tables[DAILY], key=lambda row: (row["stock_code"], row["trade_date"])):
            found.setdefault(row["stock_code"], {})[row["trade_date"]] = row["close"]
        return found

    def queries(self, table: str, kind: Optional[str] = None) -> list[FakeQuery]:
        return [q for q in self.executed if q.table == table and kind in (None, q.kind)]

    def writes(self) -> list[FakeQuery]:
        return [q for q in self.executed if q.kind != "select"]

    # ── the DB's rules ───────────────────────────────────────────────────────

    def new_row(self, table: str) -> dict:
        """A new row before its written columns: NULLs and the table defaults."""
        row = {name: None for name in COLUMNS[table]}
        if table == SNAPSHOT:
            row |= {"flags": [], "source": "KIS", "updated_at": self.now}
        return row

    def checker(self, table: str) -> Callable[[dict], None]:
        return {SNAPSHOT: self.check_snapshot, RUNS: self.check_run, DAILY: self.check_daily}[table]

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
    def check_daily(row: dict) -> None:
        for name in DAILY_COLUMNS:
            if row.get(name) is None:
                raise FakeDBError(f'null value in column "{name}" violates not-null constraint')
        try:
            date.fromisoformat(row["trade_date"])
        except (TypeError, ValueError):
            raise FakeDBError(f'invalid input syntax for type date: "{row["trade_date"]}"') from None
        close = row["close"]
        if isinstance(close, bool) or not isinstance(close, int) or not BIGINT[0] <= close <= BIGINT[1]:
            raise FakeDBError(f'invalid input syntax for type bigint: "{close}"')

    @staticmethod
    def check_snapshot(row: dict) -> None:
        for name in ("stock_code", "flags", "source", "updated_at"):
            if row.get(name) is None:
                raise FakeDBError(f'null value in column "{name}" violates not-null constraint')
        if row.get("market") not in MARKETS:
            raise FakeDBError('new row violates check constraint "chk_price_snapshot_market"')
        if not (isinstance(row["flags"], list) and all(isinstance(flag, str) for flag in row["flags"])):
            raise FakeDBError("flags must be a text array")

    @staticmethod
    def check_run(row: dict) -> None:
        for name in ("run_id", "started_at", "status"):
            if row.get(name) is None:
                raise FakeDBError(f'null value in column "{name}" violates not-null constraint')
        if row["status"] not in STATUSES:
            raise FakeDBError('new row violates check constraint "chk_price_runs_status"')


# ── KIS ──────────────────────────────────────────────────────────────────────

AS_OF = date(2026, 10, 8)   # a Thursday
QUOTE = {"market_cap": 5_000_000_000, "listed_shares": 1_000_000, "halted": False, "admin_issue": False}


def trading_rows(closes: list, *, last_day: date = AS_OF, values: Optional[list] = None) -> list[dict]:
    """KIS daily rows (oldest first) for these closes on consecutive weekdays ending at ``last_day``."""
    days, day = [], last_day
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    return [{"date": day, "close": close, "volume": 1000,
             "trading_value": 1_000_000 if values is None else values[i]}
            for i, (day, close) in enumerate(zip(days, closes))]


class FakeKis:
    """The KIS client of one run: ``daily`` rows and ``quotes`` per code, or an exception from
    ``daily_errors`` / ``quote_errors``. Every call is kept in ``calls``; ``during(call)`` runs
    inside each call (to interrupt a run). ``ensure_token()`` (the run's access token) is counted
    in ``token_asked``, not in ``calls``: it runs ``token_during()``, then raises ``token_error``
    when one is set. ``closed`` after ``close()``."""

    def __init__(self) -> None:
        self.daily: dict[str, list[dict]] = {}
        self.quotes: dict[str, dict] = {}
        self.daily_errors: dict[str, BaseException] = {}
        self.quote_errors: dict[str, BaseException] = {}
        self.calls: list[tuple] = []
        self.during: Optional[Callable[[tuple], None]] = None
        self.token_asked = 0
        self.token_error: Optional[BaseException] = None
        self.token_during: Optional[Callable[[], None]] = None
        self.closed = False

    def add(self, code: str, closes: list, *, last_day: date = AS_OF, values: Optional[list] = None,
            **quote: Any) -> None:
        self.daily[code] = trading_rows(closes, last_day=last_day, values=values)
        self.quotes[code] = QUOTE | quote

    def ensure_token(self) -> None:
        assert not self.closed, "the KIS client was used after close()"
        self.token_asked += 1
        if self.token_during is not None:
            self.token_during()
        if self.token_error is not None:
            raise self.token_error

    def daily_prices(self, code: str, start: date, end: date) -> list[dict]:
        self._called(("daily_prices", code, start, end))
        if code in self.daily_errors:
            raise self.daily_errors[code]
        return [dict(row) for row in self.daily.get(code, []) if start <= row["date"] <= end]

    def quote(self, code: str) -> dict:
        self._called(("quote", code))
        if code in self.quote_errors:
            raise self.quote_errors[code]
        return dict(self.quotes.get(code, QUOTE))

    def _called(self, call: tuple) -> None:
        assert not self.closed, "the KIS client was used after close()"
        self.calls.append(call)
        if self.during is not None:
            self.during(call)

    def codes(self, method: str) -> list[str]:
        return [call[1] for call in self.calls if call[0] == method]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeKis":
        self.closed = False   # a test may run the command twice on one stand-in
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
