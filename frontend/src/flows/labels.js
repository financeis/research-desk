// The flows screens' one place for server codes → Korean labels and number formats.
// INVESTORS, SORTS and PERIODS copy the server's value lists (research_desk/features/flows):
// when the server adds or renames a key, change it here too — an unknown key shows as itself.

export const INVESTORS = [
  ["pe_fund", "사모"],
  ["foreigner", "외국인"],
  ["institution", "기관합계"],
  ["pension", "연기금"],
  ["trust", "투신"],
  ["fin_invest", "금융투자"],
  ["insurance", "보험"],
  ["bank", "은행"],
  ["other_fin", "기타금융"],
  ["other_corp", "기타법인"],
  ["individual", "개인"],
];
export const DEFAULT_INVESTOR = "pe_fund";
// The reference rank columns of the ranking table (the source note's three columns).
export const REF_INVESTORS = ["pe_fund", "foreigner", "other_corp"];

export const SORTS = [
  ["score", "종합 점수"],
  ["rank", "250일 순위"],
  ["pct", "시총 대비 %"],
  ["amount", "순매수 금액"],
];
export const DEFAULT_SORT = "score";

export const PERIODS = [
  ["1m", "1개월"],
  ["3m", "3개월"],
  ["6m", "6개월"],
  ["1y", "1년"],
];
export const DEFAULT_PERIOD = "3m";

// Market-cap floor presets in 억 원 (the server takes any whole number 0–10,000,000).
export const CAP_PRESETS = [
  [0, "전체"],
  [500, "500억"],
  [1000, "1,000억"],
  [3000, "3,000억"],
  [10000, "1조"],
];
export const DEFAULT_MIN_CAP = 1000;
export const PAGE = 50; // rows added by "더 보기"
export const MAX_LIMIT = 200; // the server's largest limit

// The fixed 404 sentence of GET /api/stocks/{code}/flows the tab compares character for character.
export const NO_FLOW_DATA = "이 종목은 수급 자료가 없습니다.";

const INVESTOR_NAME = Object.fromEntries(INVESTORS);
export const investorName = (key) =>
  key == null
    ? "—"
    : Object.prototype.hasOwnProperty.call(INVESTOR_NAME, key)
      ? INVESTOR_NAME[key]
      : String(key);

const known = (value) =>
  value != null && value !== "" && Number.isFinite(Number(value));
const fixed = (value, digits) =>
  Number(value).toLocaleString("ko-KR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });

// Whole won → 억 with one decimal, signed amounts keep their sign ("141.5억", "-3.2억").
export function eok1(won) {
  if (!known(won)) return "—";
  const v = Math.round((Number(won) / 1e8) * 10) / 10 || 0;
  return `${fixed(v, 1)}억`;
}
// Whole won → whole 억 ("15,187억").
export function eok0(won) {
  if (!known(won)) return "—";
  return `${fixed(Math.round(Number(won) / 1e8), 0)}억`;
}
// A percent with two decimals ("0.11%").
export const pct2 = (value) => (known(value) ? `${fixed(value, 2)}%` : "—");
// A 0–100 score with one decimal.
export const score1 = (value) => (known(value) ? fixed(value, 1) : "—");
export const rankText = (value) => (known(value) ? `${Number(value)}위` : "—");
// Ranks 1–3 are highlighted like the source note's pink cells.
export const hot = (value) => known(value) && Number(value) <= 3;
export const won = (value) =>
  known(value) ? `${fixed(Math.round(Number(value)), 0)}원` : "—";

// How long the 250-day window was for that day: none under 250 days, '이력 짧음' from 60 to 249,
// '상장 N거래일' (no rank at all) under 60.
export function historyNote(windowDays) {
  if (!known(windowDays)) return "";
  const n = Number(windowDays);
  if (n < 60) return `상장 ${n}거래일`;
  if (n < 250) return `이력 짧음(${n}일)`;
  return "";
}
