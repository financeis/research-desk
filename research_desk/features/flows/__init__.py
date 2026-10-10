"""Flows (수급): the daily investor-flow strength ranking. After the daily price update, the command
``flows update`` fetches each stock's daily net buying by the 11 KRX investor groups from KIS
(millions of won), keeps the latest 310 trading days, and has the DB fix each stock's 250-trading-day
rank of the base trading day's net buying together with that day's real market cap; the web ranks a
ranked day's stocks by the mean of two percentiles (the 250-day rank, and net buying as a percent of
the market cap) and shows one stock's flows on its company screen. This feature owns the tables
``investor_flow_daily``, ``investor_flow_runs``, ``investor_flow_days`` and ``investor_flow_stocks``
and the DB functions ``rank_investor_flows`` / ``investor_flow_coverage`` (migration 009; reads and
writes in ``store.py``); its rules are the pure functions of ``logic.py``. Other features use only
the names below.

- ``latest_run()`` → ``{"last_run_at": datetime (aware, UTC; the latest run's start),
  "last_run_status": "running" | "ok" | "partial" | "failed", "as_of": date | None (the latest
  ok / partial run's base trading day)}``, or None when there has been no run: the shape of the
  prices window's ``latest_run()``. Raises ``NotReady("수급", "DB 접속 설정(SUPABASE_URL,
  SUPABASE_SERVICE_KEY)이 없습니다")`` without DB settings (tried again on the next call).
- ``register_jobs(subparsers)`` adds the command ``flows update [--codes 005930,080220]``
  (``jobs.py``): exit 0 for an ok / partial run, 1 for a failed or refused one (a price update or
  another flows update running), 4 when not ready (DB settings, KIS_APP_KEY / KIS_APP_SECRET, the
  stock list), 2 for argument errors. ``--codes`` only prints the stocks' ranks computed on this PC
  and writes nothing. Settings: the KIS account shared with the price update (KIS_APP_KEY,
  KIS_APP_SECRET, KIS_BASE_URL, KIS_TOKEN_CACHE) and FLOWS_MAX_CALLS_PER_SEC (default 10);
  ``scripts/run-prices.ps1`` runs it after the price update for the daily schedule.

Importing this window loads no FastAPI, HTTP library, KIS client, DB client or other feature's
window: ``cli.py`` imports it for every command.
"""
from .jobs import register as register_jobs
from .service import latest_run
