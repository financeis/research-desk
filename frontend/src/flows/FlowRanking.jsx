import React, { useEffect, useState } from "react";
import { Activity, LoaderCircle } from "lucide-react";
import { api } from "../api";
import { ReportsCell } from "../peers/PeerTable";
import { count, dotDate } from "../peers/labels";
import {
  CAP_PRESETS,
  DEFAULT_INVESTOR,
  DEFAULT_MIN_CAP,
  DEFAULT_SORT,
  INVESTORS,
  MAX_LIMIT,
  PAGE,
  REF_INVESTORS,
  SORTS,
  eok0,
  eok1,
  historyNote,
  hot,
  investorName,
  pct2,
  rankText,
  score1,
} from "./labels";
import "../peers/peers.css";
import "./flows.css";

function RankCell({ value }) {
  return (
    <td className={`num flow-rank ${hot(value) ? "flow-hot" : ""}`}>
      {value == null ? "—" : value}
    </td>
  );
}

// 수급 랭킹 (?view=flows): the day's investor-flow strength table of GET /api/flows. The server
// filters, scores and sorts; this screen only chooses the request and shows the rows. A row opens
// that company's page.
export default function FlowRanking({ onStock }) {
  const [investor, setInvestor] = useState(DEFAULT_INVESTOR),
    [minCap, setMinCap] = useState(DEFAULT_MIN_CAP),
    [capText, setCapText] = useState(String(DEFAULT_MIN_CAP)),
    [day, setDay] = useState(""),
    [sort, setSort] = useState(DEFAULT_SORT),
    [limit, setLimit] = useState(PAGE),
    [data, setData] = useState(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const q = new URLSearchParams({
      investor,
      min_cap: String(minCap),
      sort,
      limit: String(limit),
    });
    if (day) q.set("date", day);
    api(`/flows?${q}`, { signal: controller.signal })
      .then((x) => {
        setData(x);
        setLoading(false);
      })
      .catch((e) => {
        if (e.name !== "AbortError") {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [investor, minCap, day, sort, limit, retry]);
  // A new question starts again from the first page.
  const restart = (set) => (value) => {
    set(value);
    setLimit(PAGE);
  };
  function applyCap(text) {
    const value = Math.max(0, Math.min(10000000, Math.floor(Number(text))));
    if (!Number.isFinite(value)) return;
    setCapText(String(value));
    restart(setMinCap)(value);
  }
  function open(code) {
    onStock(code);
    window.scrollTo({ top: 0 });
  }
  const rows = data?.rows || [];
  const name = investorName(investor);
  return (
    <section className="flows-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">INVESTOR FLOWS</span>
          <h1>수급 랭킹</h1>
          <p className="page-description">
            그날 들어온 순매수가 그 종목의 최근 250거래일 중 몇 번째로
            강했는지와 시가총액 대비 몇 %였는지를 함께 보고, 강한 순서로
            줄 세웁니다.
          </p>
        </div>
      </div>
      <div className="flows-toolbar">
        <label className="flows-field">
          기준 주체
          <select
            value={investor}
            onChange={(e) => restart(setInvestor)(e.target.value)}
          >
            {INVESTORS.map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="flows-field">
          날짜
          <select
            value={day || data?.date || ""}
            onChange={(e) => restart(setDay)(e.target.value)}
            disabled={!data?.dates?.length}
          >
            {(data?.dates || []).map((d) => (
              <option key={d} value={d}>
                {dotDate(d)}
              </option>
            ))}
          </select>
        </label>
        <div className="flows-field">
          시총 하한
          <div className="segmented" role="group" aria-label="시총 하한">
            {CAP_PRESETS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={minCap === value ? "active" : ""}
                aria-pressed={minCap === value}
                onClick={() => applyCap(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <input
            type="number"
            min="0"
            step="1"
            inputMode="numeric"
            aria-label="시총 하한(억 원)"
            value={capText}
            onChange={(e) => setCapText(e.target.value)}
            onBlur={() => applyCap(capText)}
            onKeyDown={(e) => e.key === "Enter" && applyCap(capText)}
          />
          억
        </div>
        <div className="flows-field">
          정렬
          <div className="segmented" role="group" aria-label="정렬">
            {SORTS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={sort === value ? "active" : ""}
                aria-pressed={sort === value}
                onClick={() => restart(setSort)(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>
      <details className="flows-guide">
        <summary>표 읽는 법</summary>
        <ul>
          <li>
            <strong>250일 순위</strong>: 그날 순매수가 그 종목의 최근
            250거래일(그날 포함) 가운데 몇 번째로 컸는지입니다. 2위라면 그날보다
            더 크게 들어온 날이 최근 250거래일 중 하루 있었다는 뜻입니다. 1~3위는
            분홍으로 칠합니다.
          </li>
          <li>
            <strong>시총 대비 %</strong>: 그날 순매수 금액 ÷ 그날 시가총액 ×
            100.
          </li>
          <li>
            <strong>종합 점수</strong>: 250일 순위를 0~100점(1위 = 100점)으로,
            시총 대비 %를 지금 표에 들어온 종목 사이의 백분위(0~100점)로 바꿔
            평균한 값입니다. 둘 다 높아야 위에 옵니다.
          </li>
          <li>
            <strong>리포트 수</strong>: 최근 1년 증권사 종목 리포트 수입니다.
            '리포트 없음'은 증권사가 다루지 않는 종목입니다.
          </li>
          <li>
            상장한 지 60거래일이 안 된 종목은 순위를 매기지 않고, 250거래일이 안
            된 종목에는 '이력 짧음'을 붙입니다.
          </li>
          <li>
            하루 수급으로 이후 주가를 맞히는 힘은 약하다는 연구가 많습니다. 매수
            신호가 아니라 눈여겨볼 종목을 고르는 용도로 보세요.
          </li>
        </ul>
      </details>
      {error && !data ? (
        <div className="error-panel" role="alert">
          <p>{error}</p>
          <button
            className="button secondary"
            onClick={() => setRetry((x) => x + 1)}
          >
            다시 시도
          </button>
        </div>
      ) : !data ? (
        <div className="comparison-loading" role="status">
          <LoaderCircle className="spin" size={17} />
          수급 순위를 불러오고 있습니다.
        </div>
      ) : (
        <>
          <p className="flows-basis">
            <Activity size={13} /> {dotDate(data.date)} 기준 · {name} 순매수가
            들어온 종목 {count(data.total)}곳
            {loading && <LoaderCircle className="spin" size={13} />}
          </p>
          {error && (
            <div className="error-panel" role="alert">
              <p>{error}</p>
              <button
                className="button secondary"
                onClick={() => setRetry((x) => x + 1)}
              >
                다시 시도
              </button>
            </div>
          )}
          {rows.length ? (
            <div className="peer-table-wrap">
              <table className="peer-table flows-table">
                <thead>
                  <tr>
                    <th className="peer-rank">순번</th>
                    <th>회사</th>
                    <th className="num">시총</th>
                    <th className="num">
                      {name} 순매수
                    </th>
                    <th className="num">시총 대비</th>
                    <th className="num">
                      250일 순위
                      <small>{name}</small>
                    </th>
                    <th className="num">종합 점수</th>
                    {REF_INVESTORS.map((key) => (
                      <th className="num flow-ref" key={key}>
                        {investorName(key)}
                        <small>250일 순위</small>
                      </th>
                    ))}
                    <th>리포트 · 최근 1년</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr
                      key={r.code}
                      className="flow-row"
                      onClick={() => open(r.code)}
                    >
                      <td className="peer-rank">{r.no}</td>
                      <td>
                        <strong>{r.name || r.code}</strong>
                        <small className="peer-sub">
                          {r.code} · {r.market || "시장 미상"}
                        </small>
                        {r.short_history && (
                          <span className="badge peer-mark">
                            {historyNote(r.window_days)}
                          </span>
                        )}
                      </td>
                      <td className="num">{eok0(r.market_cap)}</td>
                      <td className="num">
                        <strong>{eok1(r.amount)}</strong>
                      </td>
                      <td className="num">{pct2(r.pct)}</td>
                      <td
                        className={`num flow-rank ${hot(r.rank) ? "flow-hot" : ""}`}
                        title={rankText(r.rank)}
                      >
                        {r.rank ?? "—"}
                      </td>
                      <td className="num">
                        <strong>{score1(r.score)}</strong>
                      </td>
                      {REF_INVESTORS.map((key) => (
                        <RankCell key={key} value={r.ref_ranks?.[key]} />
                      ))}
                      <ReportsCell coverage={r.coverage} />
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="panel flows-empty">
              <p>조건에 맞는 종목이 없습니다. 시총 하한이나 기준 주체를 바꿔 보세요.</p>
            </div>
          )}
          {rows.length > 0 &&
            rows.length < (data.total || 0) &&
            limit < MAX_LIMIT && (
              <button
                type="button"
                className="button secondary flows-more"
                disabled={loading}
                onClick={() => setLimit((x) => Math.min(MAX_LIMIT, x + PAGE))}
              >
                더 보기 ({count(rows.length)} / {count(Math.min(data.total, MAX_LIMIT))})
              </button>
            )}
        </>
      )}
    </section>
  );
}
