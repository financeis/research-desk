"""Report analysis: one selected report's financial analysis, and the report_summaries table.

No web routes. The reports feature reads the row and calls ``analyze_report`` and
``delete_summary``; compare uses ``ai_slot``, ``phase2_llm``, ``save_comparison`` and
``hold_reports``. Public names:

- ``analyze_report(row, force=False)`` (async) → ``(saved summary, reused)``: checks
  busy (409) → 단일종목 (422) → reuse of a saved summary with financial_details
  (skipped with ``force``) → model key / codex CLI → PDF (404 / 422) → the text or
  page-image analysis (422 for an unreadable PDF or an empty image answer) → clears the
  comparisons citing the report, then saves.
- ``delete_summary(rid)`` (async): busy (409) → clears the comparisons citing the report,
  then deletes its result. No AI slot, no model key.
- ``hold_reports(ids, busy_detail)``: async context manager marking reports busy for a
  block (409 ``busy_detail`` when one already is). Analysis, delete and this share one
  busy marker.
- ``summaries_for(ids)`` → ``{report_id: summary}``, active summary version only.
- ``save_comparison(report_id, prev_report_id, match_type, narrative, comparison_details)``.
- ``ai_slot()``: async context manager; at most 2 AI calls at once in the web
  server, shared by analysis and compare.
- ``phase2_llm()`` → ``(client, model, timeout_s)``; re-reads ``.env`` and raises
  ``NotReady("분석", …)`` when the model's API key or the codex CLI is missing.
"""
from .service import (ai_slot, analyze_report, delete_summary, hold_reports, phase2_llm,
                      save_comparison, summaries_for)
