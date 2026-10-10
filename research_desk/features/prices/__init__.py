"""Prices (주가): the daily KIS price snapshot of every stock in the stock list (spec §8), and a
year of its adjusted daily closes.

This feature owns ``stock_price_snapshot`` (one row per stock, the latest snapshot only),
``price_update_runs`` (one row per update run) and ``stock_price_daily`` (the adjusted closes,
수정 종가, one row per stock and day for the last 380 calendar days, rewritten from KIS's adjusted
series by every successful run, never adjusted here); other features read prices only through the
names below. No web routes. Units: returns and excess returns are percent floats (12.3 means
+12.3 %), in the DB and here alike; money is whole KRW.

- ``snapshots(codes)`` → ``{code: price}`` for the codes that have a stored snapshot (others are
  left out; each code once, in the order asked; codes that are not plain letters and digits match
  nothing; one plain string instead of a collection is a TypeError). ``price`` is spec §12.1's
  ``price`` plus ``market``::

      {"market": "KOSPI" | "KOSDAQ" | None, "as_of": "YYYY-MM-DD", "close": int,
       "market_cap": int, "avg_value_20d": int, "traded": bool,
       "returns": {"1w": float, "1m": float, "3m": float},
       "excess": {"1w": float, "1m": float, "3m": float},
       "flags": [...]}   # no_data / short_history / halted / admin_issue

  A missing value is None. ``excess`` = the return minus the median return of the same market in
  the run that stored it. ``no_data``: the last run could not get the stock, so the values are
  from an earlier run (see ``as_of``).
- ``latest_run()`` → ``{"last_run_at": datetime (aware, UTC; the latest run's start),
  "last_run_status": "running" | "ok" | "partial" | "failed", "as_of": date | None (the latest
  ok / partial run's data date)}``, or None when there has been no run.
- ``daily_closes(codes, since)`` → ``{code: [{"date": "YYYY-MM-DD", "close": int}, …]}``: the
  stored adjusted closes (수정 종가, whole won) of each code from ``since`` (a date or
  ``YYYY-MM-DD``, that day included) on, oldest first. Only codes with such rows are in it (each
  code once, in the order asked; codes that are not plain letters and digits match nothing; one
  plain string instead of a collection is a TypeError, a ``since`` that is not a day a TypeError
  or ValueError). A day without a row (a holiday, a halt the KIS series skips, before the
  history) is simply absent. Read 1000 rows a request, a new query for each page.

All three raise ``NotReady("주가", "DB 접속 설정(SUPABASE_URL, SUPABASE_SERVICE_KEY)이 없습니다")``
without DB settings (tried again on the next call).

- ``register_jobs(subparsers)`` adds the command ``prices update [--codes 005930,080220]``
  (``jobs.py``): exit 0 for an ok / partial run, 1 for a failed one, 4 when not ready (DB
  settings, KIS_APP_KEY / KIS_APP_SECRET, the stock list), 2 for argument errors. An ok / partial
  run also writes the received stocks' adjusted closes and deletes the history older than its
  380 days; a failed run changes neither. ``--codes`` only prints the computed rows and writes
  nothing. Settings: KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL (default the real service),
  PRICES_MAX_CALLS_PER_SEC (default 10); ``scripts/run-prices.ps1`` runs the command for the daily
  schedule.

Importing this window loads no FastAPI, HTTP library, KIS client or MongoDB package.
"""
from .jobs import register as register_jobs
from .service import daily_closes, latest_run, snapshots
