"""The screen's copies of this feature's server values (``frontend/src/flows/labels.js``).

The flows screens send investor, sort and period keys the server checks (an unknown one is a 422),
compare the 404 sentence of the company tab character for character (``NO_FLOW_DATA``: the no-data
panel instead of the general error screen), and mark short histories with the server's 60 / 250
trading days. The server lists are the source; these tests read the screen file as text and fail
when the two drift apart, so a change on one side is made on the other too:

- the investors (key and Korean name, in screen order), the default investor and the three
  reference rank columns;
- the sort keys and the default sort, the company tab's period keys and the default period;
- the default market-cap floor, the rows a page adds and the largest limit;
- the fixed 404 sentence;
- the history thresholds in ``historyNote``;
- the market-cap floor the ranking screen clamps typed values to (``FlowRanking.jsx``).
"""
from __future__ import annotations

import re
from pathlib import Path

import research_desk
from research_desk.features.flows import logic, router, service

SCREEN = Path(research_desk.__file__).resolve().parent.parent / "frontend" / "src" / "flows"
LABELS = (SCREEN / "labels.js").read_text(encoding="utf-8")
RANKING = (SCREEN / "FlowRanking.jsx").read_text(encoding="utf-8")


def _block(name: str) -> str:
    """The text inside ``export const <name> = [ ... ];`` of labels.js."""
    found = re.search(rf"export const {name} = \[(.*?)\];", LABELS, re.S)
    assert found, f"labels.js has no array {name}"
    return found.group(1)


def _string_pairs(name: str) -> list[tuple[str, str]]:
    return re.findall(r'\[\s*"([^"]+)",\s*"([^"]+)"\s*\]', _block(name))


def _scalar(name: str) -> str:
    found = re.search(rf"export const {name} = (\"[^\"]*\"|\d+);", LABELS)
    assert found, f"labels.js has no constant {name}"
    return found.group(1).strip('"')


def test_the_investors_are_the_servers_keys_and_names_in_order():
    assert _string_pairs("INVESTORS") == [(investor.key, investor.name) for investor in logic.INVESTORS]


def test_the_default_investor_and_the_reference_columns_are_the_servers():
    assert _scalar("DEFAULT_INVESTOR") == logic.DEFAULT_INVESTOR
    assert re.findall(r'"([^"]+)"', _block("REF_INVESTORS")) == list(logic.REF_INVESTORS)


def test_the_sorts_are_the_servers_keys_and_default():
    assert [key for key, _ in _string_pairs("SORTS")] == list(logic.SORTS)
    assert _scalar("DEFAULT_SORT") == logic.DEFAULT_SORT


def test_the_periods_are_the_servers_keys_and_default():
    assert [key for key, _ in _string_pairs("PERIODS")] == list(service.PERIODS)
    assert _scalar("DEFAULT_PERIOD") == service.DEFAULT_PERIOD


def test_the_floor_page_and_limit_are_the_servers():
    assert int(_scalar("DEFAULT_MIN_CAP")) == logic.DEFAULT_MIN_CAP
    assert int(_scalar("PAGE")) == logic.DEFAULT_LIMIT
    assert int(_scalar("MAX_LIMIT")) == router.MAX_LIMIT
    presets = [int(value) for value in re.findall(r"\[\s*(\d+),", _block("CAP_PRESETS"))]
    assert presets and all(0 <= value <= router.MAX_MIN_CAP for value in presets)
    assert logic.DEFAULT_MIN_CAP in presets


def test_the_no_data_sentence_is_the_servers_404_sentence():
    assert _scalar("NO_FLOW_DATA") == router.NO_FLOW_DATA


def test_the_history_marks_use_the_servers_day_counts():
    assert re.findall(r"if \(n < (\d+)\)", LABELS) == [str(logic.MIN_DAYS), str(logic.WINDOW_DAYS)]


def test_the_ranking_screen_clamps_the_floor_to_the_servers_largest_value():
    assert re.findall(r"Math\.min\((\d+),", RANKING) == [str(router.MAX_MIN_CAP)]
