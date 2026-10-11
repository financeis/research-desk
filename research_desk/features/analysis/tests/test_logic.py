"""Post-processing and page-image rules (no DB, no network).

- Target price direction is recomputed by arithmetic (ported from
  langgraph_tagger/analytics/llm_summary/tests/test_pipeline.py).
- Page-image analysis: the whole-document decision, the empty-result rule and
  the confidence cap.
"""
import pytest

from research_desk.features.analysis.financials import (
    Evidence, FinancialDetails, FinancialMetric, RatingDetails, Valuation,
)
from research_desk.features.analysis.logic import (
    PAGE_IMAGE_MAX_HANGUL_PER_PAGE, PAGE_IMAGE_MAX_LATIN_PER_PAGE,
    cap_image_confidence, is_empty_image_result, needs_page_images,
    normalize_target_price_dir,
)
from research_desk.features.analysis.schemas import ExtractionResult


def _make(new=None, old=None, dir_='N/A'):
    return ExtractionResult(
        target_price_new=new, target_price_old=old,
        target_price_dir=dir_, recommendation='매수',
        recommendation_dir='유지', one_line_summary='X',
        positive_points=[], risk_points=[],
        target_price_raw='8만원' if new else None,
        recommendation_raw='Buy',
        source_pages=[1] if new else [],
        extraction_confidence='high',
    )


def test_normalize_both_int_higher_overrides_to_상향():
    r = _make(new=85000, old=70000, dir_='불변')  # LLM이 틀려도
    out = normalize_target_price_dir(r)
    assert out.target_price_dir == '상향'


def test_normalize_both_int_lower_overrides_to_하향():
    r = _make(new=60000, old=70000, dir_='상향')  # LLM이 틀려도
    out = normalize_target_price_dir(r)
    assert out.target_price_dir == '하향'


def test_normalize_both_int_equal_overrides_to_불변():
    r = _make(new=70000, old=70000, dir_='상향')
    out = normalize_target_price_dir(r)
    assert out.target_price_dir == '불변'


def test_normalize_only_new_keeps_llm_judgment():
    r = _make(new=85000, old=None, dir_='신규')
    out = normalize_target_price_dir(r)
    assert out.target_price_dir == '신규'  # LLM 판단 유지


def test_normalize_neither_forces_NA():
    r = _make(new=None, old=None, dir_='상향')  # LLM 잘못 추출
    out = normalize_target_price_dir(r)
    assert out.target_price_dir == 'N/A'


# ── page-image decision (business-rules 리포트 1건 재무 분석) ───────────────────────────────────────────
# Whole document, averaged over every page: image path only when
# Hangul avg < 50 AND Latin avg < 300 (exactly equal → text path).

def _page(hangul=0, latin=0, other=''):
    return '가' * hangul + 'a' * latin + other


def test_page_image_thresholds_are_the_spec_values():
    assert PAGE_IMAGE_MAX_HANGUL_PER_PAGE == 50
    assert PAGE_IMAGE_MAX_LATIN_PER_PAGE == 300


def test_hangul_average_just_under_the_limit_is_images():
    pages = [_page(hangul=49)] * 9 + [_page(hangul=58)]   # 499 / 10 = 49.9
    assert needs_page_images(pages) is True


def test_hangul_average_exactly_at_the_limit_is_text():
    pages = [_page(hangul=49)] * 9 + [_page(hangul=59)]   # 500 / 10 = 50
    assert needs_page_images(pages) is False


def test_latin_average_just_under_the_limit_is_images():
    assert needs_page_images([_page(latin=299)]) is True


def test_latin_average_exactly_at_the_limit_is_text():
    assert needs_page_images([_page(latin=300)]) is False
    assert needs_page_images([_page(latin=299), _page(latin=301)]) is False  # avg 300


def test_english_report_is_text():
    pages = [_page(latin=1800, other=' 2026F 12,345 ') for _ in range(8)]
    assert needs_page_images(pages) is False


def test_measured_body_missing_shapes_are_images():
    # Measured: PDFs whose body text is missing had Hangul 0-38, Latin ~50 or less.
    pages = [_page(hangul=h, latin=50, other=' 2026 1,234 (%) ') for h in (0, 12, 38, 25)]
    assert needs_page_images(pages) is True


def test_measured_normal_shape_is_text():
    # Measured: normal reports had Hangul 76 or more per page.
    pages = [_page(hangul=76, latin=50)] * 5
    assert needs_page_images(pages) is False


def test_average_is_over_all_pages_including_empty_ones():
    # One text-heavy page among empty (scanned) pages: the empty pages pull
    # the average down, they are not skipped.
    assert needs_page_images([_page(hangul=150), '', '']) is False   # 150 / 3 = 50
    assert needs_page_images([_page(hangul=149), '', '']) is True    # 149 / 3 < 50
    assert needs_page_images([_page(latin=899), '', '']) is True     # 899 / 3 < 300
    assert needs_page_images([_page(latin=900), '', '']) is False    # 900 / 3 = 300


def test_both_limits_must_hold_for_images():
    assert needs_page_images([_page(hangul=10, latin=400)]) is False
    assert needs_page_images([_page(hangul=80, latin=10)]) is False


def test_only_hangul_syllables_and_ascii_letters_count():
    # Jamo, digits, punctuation, full-width Latin and other scripts are not counted.
    noise = 'ㄱㅏ' * 100 + '0123456789' * 100 + 'ＡＢＣ' * 200 + '株式' * 100 + '.,()%' * 100
    assert needs_page_images([noise]) is True


def test_no_pages_is_not_images():
    # The service rejects a PDF with no pages (422) before deciding.
    assert needs_page_images([]) is False


# ── empty image result and confidence cap (business-rules 리포트 1건 재무 분석) ────────────

def _metric():
    return FinancialMetric(
        metric='매출액', fiscal_period='2026', value=1234.0, previous_value=None,
        unit='십억원', currency='KRW', accounting_basis='연결', value_type='추정',
        scenario='기본', evidence=Evidence(page=1, quote='매출액 2026F 1,234'),
    )


def _details(metrics):
    return FinancialDetails(
        metrics=metrics,
        valuation=Valuation(method='미기재', target_horizon=None, explanation=None),
        rating=RatingDetails(current_label=None, previous_label=None,
                             definition=None, horizon=None, evidence=None),
    )


def _image_result(**overrides):
    fields = dict(
        financial_details=_details([]), target_price_new=None, target_price_old=None,
        target_price_dir='N/A', recommendation='N/A', recommendation_dir='N/A',
        one_line_summary='읽을 수 없음', positive_points=[], risk_points=[],
        extraction_confidence='low',
    )
    fields.update(overrides)
    return ExtractionResult(**fields)


def test_empty_image_result_when_all_four_hold():
    assert is_empty_image_result(_image_result()) is True


def test_missing_financial_details_counts_as_no_metrics():
    assert is_empty_image_result(_image_result(financial_details=None)) is True


@pytest.mark.parametrize('overrides', [
    {'target_price_new': 80000, 'target_price_raw': '8만원'},
    {'recommendation': '매수'},
    {'financial_details': _details([_metric()])},
    {'extraction_confidence': 'medium'},
    {'extraction_confidence': 'high'},
], ids=['target_price', 'recommendation', 'one_metric', 'medium', 'high'])
def test_not_empty_when_any_one_condition_fails(overrides):
    assert is_empty_image_result(_image_result(**overrides)) is False


def test_cap_lowers_high_to_medium():
    r = _image_result(extraction_confidence='high')
    out = cap_image_confidence(r)
    assert out.extraction_confidence == 'medium'
    assert r.extraction_confidence == 'high'          # the input is not changed
    assert out.model_dump(exclude={'extraction_confidence'}) == \
        r.model_dump(exclude={'extraction_confidence'})


@pytest.mark.parametrize('level', ['medium', 'low'])
def test_cap_keeps_medium_and_low(level):
    r = _image_result(extraction_confidence=level)
    assert cap_image_confidence(r).extraction_confidence == level
