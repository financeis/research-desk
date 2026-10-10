import React from "react";
import { eok1, won } from "./labels";

const LEFT = 60,
  RIGHT = 660,
  TOP = 16,
  BOTTOM = 226;
const UP = "#d0473a",
  DOWN = "#3b6fb6",
  CUMULATIVE = "#2f3a45",
  CLOSE = "#c48a1e";

const tickWon = (value) => {
  const v = value / 1e8;
  const digits = Math.abs(v) < 10 ? 1 : 0;
  return `${v.toLocaleString("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits })}억`;
};

// One investor's daily net buying as bars (red in, blue out), the period's cumulative net buying
// as a dark line on the same left axis (both in won), and the adjusted close as a line on the
// right axis. A day without a close breaks the price line.
export default function FlowChart({ dates, values, close, name }) {
  const n = dates.length;
  if (!n) return <p className="chart-empty">이 기간의 수급 자료가 없습니다.</p>;
  const cumulative = [];
  values.reduce((sum, v, i) => (cumulative[i] = sum + (v || 0)), 0);
  const lo = Math.min(0, ...values.map((v) => v || 0), ...cumulative),
    hi0 = Math.max(0, ...values.map((v) => v || 0), ...cumulative),
    hi = hi0 === lo ? lo + 1 : hi0;
  const width = RIGHT - LEFT,
    height = BOTTOM - TOP,
    step = width / n;
  const x = (i) => LEFT + (i + 0.5) * step;
  const yLeft = (v) => TOP + ((hi - v) / (hi - lo)) * height;
  const prices = close.filter((c) => c != null);
  const cMin0 = prices.length ? Math.min(...prices) : 0,
    cMax0 = prices.length ? Math.max(...prices) : 1,
    pad = (cMax0 - cMin0) * 0.05 || Math.max(1, cMax0 * 0.05),
    cMin = cMin0 - pad,
    cMax = cMax0 + pad;
  const yRight = (c) => TOP + ((cMax - c) / (cMax - cMin)) * height;
  // The close line in pieces: a missing close ends one piece.
  const pieces = [];
  let piece = [];
  close.forEach((c, i) => {
    if (c == null) {
      if (piece.length) pieces.push(piece);
      piece = [];
    } else piece.push(`${x(i)},${yRight(c)}`);
  });
  if (piece.length) pieces.push(piece);
  const barWidth = Math.max(1, step * 0.7);
  const leftTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => lo + (hi - lo) * f);
  const rightTicks = prices.length ? [0, 0.5, 1].map((f) => cMin0 + (cMax0 - cMin0) * f) : [];
  const labelEvery = Math.max(1, Math.ceil(n / 5));
  return (
    <div className="flow-chart">
      <svg viewBox="0 0 720 262" role="img" aria-label={`${name} 일별 순매수와 누적, 종가`}>
        {leftTicks.map((v) => (
          <g key={`l${v}`}>
            <line x1={LEFT} x2={RIGHT} y1={yLeft(v)} y2={yLeft(v)} stroke="#edf0f3" />
            <text x={LEFT - 6} y={yLeft(v) + 4} textAnchor="end" fill="#95a3ad" fontSize="10">
              {tickWon(v)}
            </text>
          </g>
        ))}
        {rightTicks.map((c) => (
          <text key={`r${c}`} x={RIGHT + 6} y={yRight(c) + 4} fill={CLOSE} fontSize="10">
            {Math.round(c).toLocaleString("ko-KR")}
          </text>
        ))}
        <line x1={LEFT} x2={RIGHT} y1={yLeft(0)} y2={yLeft(0)} stroke="#b7c0c8" />
        {values.map((v, i) => {
          const value = v || 0,
            y0 = yLeft(0),
            y1 = yLeft(value);
          return (
            <rect
              key={dates[i]}
              x={x(i) - barWidth / 2}
              y={Math.min(y0, y1)}
              width={barWidth}
              height={Math.max(0.5, Math.abs(y1 - y0))}
              fill={value >= 0 ? UP : DOWN}
              opacity="0.85"
            >
              <title>
                {`${dates[i]} · 순매수 ${eok1(value)} · 누적 ${eok1(cumulative[i])} · 종가 ${won(close[i])}`}
              </title>
            </rect>
          );
        })}
        <polyline
          fill="none"
          stroke={CUMULATIVE}
          strokeWidth="1.8"
          strokeLinejoin="round"
          points={cumulative.map((c, i) => `${x(i)},${yLeft(c)}`).join(" ")}
        />
        {pieces.map((points, i) => (
          <polyline
            key={i}
            fill="none"
            stroke={CLOSE}
            strokeWidth="1.6"
            strokeDasharray="4 2"
            strokeLinejoin="round"
            points={points.join(" ")}
          />
        ))}
        {dates.map(
          (d, i) =>
            (i % labelEvery === 0 || i === n - 1) && (
              <text key={`d${d}`} x={x(i)} y="250" textAnchor="middle" fill="#95a3ad" fontSize="10">
                {d.slice(2).replaceAll("-", ".")}
              </text>
            ),
        )}
      </svg>
      <div className="chart-legend flow-legend">
        <span>
          <i style={{ background: UP }} />
          <i style={{ background: DOWN }} />
          {name} 일별 순매수
        </span>
        <span>
          <i className="flow-line" style={{ background: CUMULATIVE }} />
          누적 순매수
        </span>
        <span>
          <i className="flow-line" style={{ background: CLOSE }} />
          수정 종가(오른쪽 축)
        </span>
      </div>
    </div>
  );
}
