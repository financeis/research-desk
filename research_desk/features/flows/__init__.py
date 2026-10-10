"""Flows (수급): the daily investor-flow strength ranking. After the daily price update, the command
``flows update`` fetches each stock's daily net buying by the 11 KRX investor groups from KIS
(millions of won), keeps the latest 310 trading days, and has the DB fix each stock's 250-trading-day
rank of the base trading day's net buying together with that day's real market cap; the web ranks a
ranked day's stocks by the mean of two percentiles (the 250-day rank, and net buying as a percent of
the market cap) and shows one stock's flows on its company screen. This feature will own the tables
``investor_flow_daily``, ``investor_flow_runs``, ``investor_flow_days`` and ``investor_flow_stocks``;
its rules are the pure functions of ``logic.py``. No public name is bound here yet.
"""
