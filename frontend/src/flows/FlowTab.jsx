import React, { useEffect, useState } from "react";
import { LoaderCircle, SearchX } from "lucide-react";
import { api } from "../api";
import { dotDate } from "../peers/labels";
import FlowChart from "./FlowChart";
import {
  DEFAULT_INVESTOR,
  DEFAULT_PERIOD,
  NO_FLOW_DATA,
  PERIODS,
  eok0,
  eok1,
  historyNote,
  hot,
  investorName,
  pct2,
} from "./labels";
import "../peers/peers.css";
import "./flows.css";

// The company page's 수급 tab (GET /api/stocks/{code}/flows): the eleven investors' net buying,
// share of market cap and 250-day rank on the latest ranked day, and one investor's daily net
// buying with its cumulative line and the adjusted close. A table row picks the chart's investor.
export default function FlowTab({ code }) {
  const [period, setPeriod] = useState(DEFAULT_PERIOD),
    [investor, setInvestor] = useState(DEFAULT_INVESTOR),
    [data, setData] = useState(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api(`/stocks/${encodeURIComponent(code)}/flows?period=${period}`, {
      signal: controller.signal,
    })
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
  }, [code, period, retry]);

  if (error === NO_FLOW_DATA)
    return (
      <section className="peers-section">
        <div className="panel peers-empty">
          <SearchX size={28} />
          <h3>{error}</h3>
          <p>
            새로 상장했거나 아직 수급을 받지 않은 종목일 수 있습니다. 매일 장
            마감 뒤 자동으로 받습니다.
          </p>
        </div>
      </section>
    );
  if (!data)
    return (
      <section className="peers-section">
        {error ? (
          <div className="error-panel" role="alert">
            <p>{error}</p>
            <button
              className="button secondary"
              onClick={() => setRetry((x) => x + 1)}
            >
              다시 시도
            </button>
          </div>
        ) : (
          <div className="comparison-loading" role="status">
            <LoaderCircle className="spin" size={17} />
            수급을 불러오고 있습니다.
          </div>
        )}
      </section>
    );

  const series = data.series || { dates: [], close: [], flows: {} };
  const note = historyNote(data.window_days);
  return (
    <section className="peers-section">
      <div className="peers-heading">
        <div>
          <span className="eyebrow">INVESTOR FLOWS</span>
          <h2>수급</h2>
          <p className="peers-basis">
            {data.as_of
              ? `${dotDate(data.as_of)} 기준 · 시총 ${eok0(data.market_cap)}`
              : "아직 순위를 매긴 날이 없습니다"}
            {note && <span className="badge peer-mark flow-note">{note}</span>}
          </p>
        </div>
        <div className="segmented" role="group" aria-label="그래프 기간">
          {PERIODS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={period === value ? "active" : ""}
              aria-pressed={period === value}
              onClick={() => setPeriod(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {error && (
        <div className="error-panel" role="alert">
          <p>{error}</p>
        </div>
      )}
      <div className="flows-tab-grid">
        <div className="peer-table-wrap">
          <table className="peer-table flows-investors">
            <thead>
              <tr>
                <th>주체</th>
                <th className="num">순매수</th>
                <th className="num">시총 대비</th>
                <th className="num">
                  250일 순위
                  <small>1~3위 강조</small>
                </th>
              </tr>
            </thead>
            <tbody>
              {(data.investors || []).map((row) => (
                <tr
                  key={row.key}
                  className={`flow-row ${row.key === investor ? "active" : ""}`}
                  onClick={() => setInvestor(row.key)}
                  aria-selected={row.key === investor}
                >
                  <td>{row.name || investorName(row.key)}</td>
                  <td className="num">{eok1(row.amount)}</td>
                  <td className="num">{pct2(row.pct)}</td>
                  <td className={`num flow-rank ${hot(row.rank) ? "flow-hot" : ""}`}>
                    {row.rank ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel flows-chart-panel">
          <h3>
            {investorName(investor)} 일별 순매수
            {loading && <LoaderCircle className="spin" size={13} />}
          </h3>
          <FlowChart
            dates={series.dates || []}
            values={series.flows?.[investor] || []}
            close={series.close || []}
            name={investorName(investor)}
          />
        </div>
      </div>
    </section>
  );
}
