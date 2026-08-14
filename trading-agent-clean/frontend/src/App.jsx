import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  API_BASE, attachTradingViewTab, detachTradingViewTab,
  getAiDataCollectionStatus, getAiFeatureDatasetSummary, getAiFeatureSnapshots, getAiOutcomePreview, getDashboardPaperEquity, getHealth, getMarketDataSymbol, getMarketLoadProgress, getMomentumCandidates, getMomentumPrecheck,
  getMomentumSummary, getMomentumTvConfirmed, getPaperHistory, getPaperOpenTrades, getPaperSummary, getPaperUpdateLock, getPaperUpdateProgress, getPaperUpdateRuns, getPaperUpdateSchedulerStatus, getScanRows, getScoreSummary, getSettings, getSwingCandidates, getSystemRuntimeInfo,
  getSwingPrecheck, getSwingSummary, getSwingTvConfirmed, getTradingViewAttachableTabs, getTradingViewRuntimeStatus, isRequestCancellation, momentumTvConfirm, loadAllMarketData, runPaperPipeline, runScoring,
  swingTvConfirm, testTvSymbol, tradingViewBadge, deriveTradingViewBusy, isBatchReady, canStartTradingViewOperation, buildTradingViewUrl,
} from "./api";
import { aiDataCollectionChecklist, aiOutcomeEligibleRows, aiOutcomeSkippedRows, aiSnapshotDisplayRows } from "./aiDataset";
import { confirmationTimestampLabel } from "./timestampUtils";

const NAV_ITEMS = [
  { label: "Dashboard", icon: "📊", group: "MAIN" },
  { label: "Swing Trading", icon: "📈", group: "TRADING" },
  { label: "Momentum Trading", icon: "🚀", group: "TRADING" },
  { label: "Paper Trades", icon: "📜", group: "TRADING" },
  { label: "Stock Detail", icon: "🔍", group: "TRADING" },
  { label: "Market Data", icon: "🌍", group: "TRADING" },
  { label: "Data Collection", icon: "🗂️", group: "DATA / AI" },
  { label: "AI Dataset / Labels", icon: "🧠", group: "DATA / AI" },
  { label: "System Health", icon: "🩺", group: "SYSTEM" },
  { label: "Settings", icon: "⚙️", group: "SYSTEM" },
];
const NAV_WITH_STOCK_DETAIL = NAV_ITEMS;

const arr = (value, keys = []) => {
  if (Array.isArray(value)) return value;
  const key = keys.find((name) => Array.isArray(value?.[name]));
  return key ? value[key] : [];
};
const settledValue = (result, fallback = null) => result?.status === "fulfilled" ? result.value : fallback;
const settledErrors = (entries) => entries
  .filter((entry) => entry.result?.status === "rejected" && !isRequestCancellation(entry.result.reason))
  .map((entry) => ({ endpoint: entry.endpoint, message: entry.result.reason?.message || String(entry.result.reason) }));
const hasValue = (value) => {
  if (value === undefined || value === null) return false;
  if (typeof value === "string") {
    const clean = value.trim();
    return clean !== "" && clean !== "-";
  }
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
};
const displayValue = (value) => {
  if (!hasValue(value)) return "";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};
const val = (value) => {
  if (value === undefined || value === null || value === "") return "-";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};
const fmt = (value) => Number.isFinite(Number(value)) ? Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 }) : val(value);
const money = (value) => Number.isFinite(Number(value)) ? `₹${Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 })}` : val(value);
const pct = (value) => Number.isFinite(Number(value)) ? `${Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 })}%` : val(value);
const num = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
const countValue = (value) => Math.max(0, Number(value) || 0);
const positiveCount = (value) => Math.max(1, Number(value) || 1);
const clampLimit = (value, max) => Math.max(1, Math.min(positiveCount(max), Number(value) || 1));
const hasFieldValue = (data, fields) => fields.some((field) => hasValue(data?.[field]));
const statusTone = (status = "") => {
  const text = String(status || "").toUpperCase();
  if (text.includes("REJECTED") || text.includes("STOP") || text.includes("CONFLICT") || text === "HIGH") return "red";
  if (text.includes("TECHNICAL_FAILED") || text.includes("FAILED")) return "gray";
  if (text.includes("CONFIRMED_SIGNAL") || text.includes("CONFIRMED") || text.includes("TARGET") || text.includes("ACTIVE") || text.includes("READY") || text.includes("VALID") || text.includes("BULLISH") || text === "LOW") return "green";
  if (text.includes("WAIT") || text.includes("PLAN") || text.includes("WATCH") || text.includes("NEUTRAL") || text === "MEDIUM") return "yellow";
  return "green";
};
const normalizeTradeQualityGrade = (grade = "") => {
  const text = String(grade || "").trim().toUpperCase().replace(/[\s-]+/g, "_");
  if (text === "A+" || text === "A_PLUS") return "A_PLUS";
  if (text === "A") return "A";
  if (text === "B") return "B";
  if (text === "C") return "C";
  if (text === "NO_TRADE" || text === "NO TRADE") return "NO_TRADE";
  return "";
};
const qualityLabel = (grade = "") => normalizeTradeQualityGrade(grade) === "A_PLUS" ? "A+" : val(normalizeTradeQualityGrade(grade) || grade);
const qualityTone = (grade = "") => {
  const text = normalizeTradeQualityGrade(grade);
  if (text === "A_PLUS" || text === "A") return "green";
  if (text === "B" || text === "C") return "yellow";
  if (text === "NO_TRADE") return "red";
  return "gray";
};
function qualityRank(row) {
  const grade = normalizeTradeQualityGrade(row?.trade_quality_grade ?? row?.quality_grade ?? row);
  if (grade === "A_PLUS") return 1;
  if (grade === "A") return 2;
  if (grade === "B") return 3;
  if (grade === "C") return 4;
  if (grade === "NO_TRADE") return 5;
  return 6;
}
function sortByTradeQuality(rows) {
  return [...(Array.isArray(rows) ? rows : [])].sort((a, b) => {
    const rankDiff = qualityRank(a) - qualityRank(b);
    if (rankDiff) return rankDiff;
    const confidenceDiff = Number(b?.confidence_score || 0) - Number(a?.confidence_score || 0);
    if (confidenceDiff) return confidenceDiff;
    const qualityScoreDiff = Number(b?.quality_score || 0) - Number(a?.quality_score || 0);
    if (qualityScoreDiff) return qualityScoreDiff;
    const aSymbol = String(a?.symbol || a?.canonical_symbol || a?.tradingview_symbol || "");
    const bSymbol = String(b?.symbol || b?.canonical_symbol || b?.tradingview_symbol || "");
    return aSymbol.localeCompare(bSymbol);
  });
}

function Badge({ tone = "green", children }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
function Card({ title, eyebrow, children, className = "" }) {
  return <section className={`card ${className}`}><div className="cardHeader"><div>{eyebrow && <span>{eyebrow}</span>}<h2>{title}</h2></div></div>{children}</section>;
}
function StatCard({ label, value, tone = "green" }) {
  return <div className={`statCard accent-${tone}`}><span>{label}</span><strong>{val(value)}</strong></div>;
}
function ActionButton({ children, onClick, disabled }) {
  return <button className="actionButton" type="button" onClick={onClick} disabled={disabled}>{children}</button>;
}
function Debug({ data }) {
  return data ? <details className="debug"><summary>Last raw JSON</summary><pre>{JSON.stringify(data, null, 2)}</pre></details> : null;
}
const LONG_TEXT_COLUMN_PATTERN = /(reason|explanation|summary|json|comment|breakdown|warning|error|message|fields)/i;

class PageErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error(`page render failed: ${this.props.pageKey}`, error, info);
  }

  componentDidUpdate(prevProps) {
    if (prevProps.pageKey !== this.props.pageKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="errorPanel">
        <strong>Page failed to render: {this.props.pageKey}</strong>
        <p>{this.state.error?.message || String(this.state.error)}</p>
      </div>
    );
  }
}

function MiniTable({ rows = [], columns = ["symbol", "score", "status"] }) {
  const safeRows = Array.isArray(rows) ? rows : [];
  return <div className="tableShell results-table-wrap"><table><thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead><tbody>
    {safeRows.length ? safeRows.map((row, i) => <tr key={`${row?.symbol || row?.tradingview_symbol || i}-${i}`}>{columns.map((c) => {
      const value = row?.[c];
      if (LONG_TEXT_COLUMN_PATTERN.test(c)) return <td key={c} className="wideText">{val(value)}</td>;
      if (c === "trade_quality_grade") return <td key={c}><Badge tone={qualityTone(value)}>{qualityLabel(value)}</Badge></td>;
      if (c.includes("score")) return <td key={c}><span className="scoreBadge">{fmt(value)}</span></td>;
      if (c.includes("status")) return <td key={c}><Badge tone={statusTone(String(value))}>{val(value)}</Badge></td>;
      if (c.includes("risk")) return <td key={c}><Badge tone={statusTone(String(value))}>{val(value)}</Badge></td>;
      return <td key={c}>{fmt(value)}</td>;
    })}</tr>) : <tr><td colSpan={columns.length}>No data loaded yet.</td></tr>}
  </tbody></table></div>;
}

function MiniLineChart({ points = [], valueKey = "value" }) {
  const rows = (Array.isArray(points) ? points : []).filter((row) => Number.isFinite(Number(row?.[valueKey])));
  if (!rows.length) return <div className="noCandlesPanel">No chart data available.</div>;
  const width = 760;
  const height = 230;
  const pad = 28;
  const values = rows.map((row) => Number(row[valueKey]));
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const span = Math.max(maxValue - minValue, 1);
  const xFor = (index) => rows.length === 1 ? width / 2 : pad + (index * (width - pad * 2)) / (rows.length - 1);
  const yFor = (value) => pad + ((maxValue - value) / span) * (height - pad * 2);
  const polyline = rows.map((row, index) => `${xFor(index)},${yFor(Number(row[valueKey]))}`).join(" ");
  const last = rows[rows.length - 1];
  return <div className="chartFrame dashboardChartFrame">
    <svg className="dashboardChart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Equity curve">
      {[0, 1, 2, 3].map((step) => {
        const y = pad + (step * (height - pad * 2)) / 3;
        return <line className="gridLine" key={step} x1={pad} x2={width - pad} y1={y} y2={y} />;
      })}
      <polyline className="dashboardLine" points={polyline} />
      {rows.map((row, index) => <circle className="dashboardPoint" key={`${row?.symbol || index}-${index}`} cx={xFor(index)} cy={yFor(Number(row[valueKey]))} r={index === rows.length - 1 ? 4 : 2.5} />)}
      <text className="chartLabel" x={pad} y={18}>{money(maxValue)}</text>
      <text className="chartLabel" x={pad} y={height - 6}>{money(minValue)}</text>
    </svg>
    <p className="chartMeta">Latest: {money(last?.[valueKey])} {last?.date ? `on ${last.date}` : ""}</p>
  </div>;
}

function MiniBarChart({ rows = [] }) {
  const safeRows = (Array.isArray(rows) ? rows : []).filter((row) => Number.isFinite(Number(row?.pnl)));
  if (!safeRows.length) return <div className="noCandlesPanel">No monthly P&L data available.</div>;
  const width = 760;
  const height = 230;
  const pad = 28;
  const values = safeRows.map((row) => Number(row.pnl));
  const maxAbs = Math.max(...values.map((value) => Math.abs(value)), 1);
  const zeroY = height / 2;
  const barStep = (width - pad * 2) / safeRows.length;
  const barWidth = Math.max(10, barStep * 0.56);
  return <div className="chartFrame dashboardChartFrame">
    <svg className="dashboardChart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Monthly P&L">
      <line className="gridLine" x1={pad} x2={width - pad} y1={zeroY} y2={zeroY} />
      {safeRows.map((row, index) => {
        const value = Number(row.pnl);
        const barHeight = Math.abs(value) / maxAbs * (height / 2 - pad);
        const x = pad + index * barStep + (barStep - barWidth) / 2;
        const y = value >= 0 ? zeroY - barHeight : zeroY;
        return <g key={`${row.month}-${index}`} className={value >= 0 ? "dashboardBarUp" : "dashboardBarDown"}>
          <rect x={x} y={y} width={barWidth} height={Math.max(2, barHeight)} rx="3" />
          <text className="chartLabel" x={x} y={height - 8}>{String(row.month || "").slice(2)}</text>
        </g>;
      })}
    </svg>
    <p className="chartMeta">Latest month: {money(safeRows[safeRows.length - 1]?.pnl)}</p>
  </div>;
}

function normalizeCandles(candles) {
  return (Array.isArray(candles) ? candles : [])
    .map((candle) => ({
      time: candle?.time,
      open: num(candle?.open),
      high: num(candle?.high),
      low: num(candle?.low),
      close: num(candle?.close),
      volume: num(candle?.volume),
    }))
    .filter((candle) => (
      candle.open !== null
      && candle.high !== null
      && candle.low !== null
      && candle.close !== null
      && candle.high >= candle.low
    ));
}

function CandleChart({ candles }) {
  const recent = normalizeCandles(candles).slice(-60);
  if (!recent.length) {
    return <div className="noCandlesPanel">No valid candles found in the TradingView response.</div>;
  }

  const width = 900;
  const priceHeight = 250;
  const volumeHeight = 74;
  const gap = 18;
  const height = priceHeight + volumeHeight + gap;
  const pad = 24;
  const minLow = Math.min(...recent.map((candle) => candle.low));
  const maxHigh = Math.max(...recent.map((candle) => candle.high));
  const priceRange = Math.max(maxHigh - minLow, 1);
  const maxVolume = Math.max(...recent.map((candle) => candle.volume || 0), 1);
  const step = (width - pad * 2) / recent.length;
  const bodyWidth = Math.max(4, step * 0.58);
  const priceY = (price) => pad + ((maxHigh - price) / priceRange) * (priceHeight - pad * 1.5);
  const volumeY = (volume) => priceHeight + gap + volumeHeight - ((volume || 0) / maxVolume) * volumeHeight;

  return (
    <div className="chartFrame">
      <svg className="candleChart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="TradingView candle chart">
        <line x1={pad} y1={priceY(maxHigh)} x2={width - pad} y2={priceY(maxHigh)} className="gridLine" />
        <line x1={pad} y1={priceY(minLow)} x2={width - pad} y2={priceY(minLow)} className="gridLine" />
        {recent.map((candle, index) => {
          const x = pad + index * step + step / 2;
          const up = candle.close >= candle.open;
          const colorClass = up ? "up" : "down";
          const bodyTop = Math.min(priceY(candle.open), priceY(candle.close));
          const bodyHeight = Math.max(Math.abs(priceY(candle.open) - priceY(candle.close)), 2);
          const volTop = volumeY(candle.volume);
          return (
            <g key={`${candle.time || index}-${index}`} className={colorClass}>
              <line x1={x} x2={x} y1={priceY(candle.high)} y2={priceY(candle.low)} className="wick" />
              <rect x={x - bodyWidth / 2} y={bodyTop} width={bodyWidth} height={bodyHeight} rx="1.5" className="body" />
              <rect x={x - bodyWidth / 2} y={volTop} width={bodyWidth} height={priceHeight + gap + volumeHeight - volTop} className="volumeBar" />
            </g>
          );
        })}
        <text x={pad} y={16} className="chartLabel">High {maxHigh.toFixed(2)}</text>
        <text x={pad} y={priceHeight - 2} className="chartLabel">Low {minLow.toFixed(2)}</text>
      </svg>
    </div>
  );
}

function LastCandleCard({ candle }) {
  if (!candle) return <div className="noCandlesPanel">Last candle unavailable.</div>;
  return <div className="ohlcvGrid">
    <div><span>Open</span><strong>{val(candle.open)}</strong></div>
    <div><span>High</span><strong>{val(candle.high)}</strong></div>
    <div><span>Low</span><strong>{val(candle.low)}</strong></div>
    <div><span>Close</span><strong>{val(candle.close)}</strong></div>
    <div><span>Volume</span><strong>{val(candle.volume)}</strong></div>
  </div>;
}

function CandidateList({ title, rows }) {
  const safeRows = Array.isArray(rows) ? rows : [];
  return <Card title={title} eyebrow="Live paper universe" className="candidateCard"><div className="candidateList">
    {safeRows.length ? safeRows.map((row, i) => <div className="candidateRow" key={`${row.symbol || i}-${i}`}>
      <div><strong>{row.tradingview_symbol || row.symbol}</strong><span>{row.status || "PAPER WATCH"}</span></div>
      <span className="scoreBadge">{val(row.score ?? row.momentum_score)}</span>
    </div>) : <p className="muted">No candidates loaded.</p>}
  </div></Card>;
}
function SetupGrid({ items }) {
  return <div className="setupGrid">{items.map((item) => <div className={`setupCard accent-${item.tone}`} key={item.label}><span>{item.label}</span><strong>{item.value}</strong><Badge tone={item.tone}>{item.status}</Badge></div>)}</div>;
}

const boolLabel = (value) => value === true ? "true" : value === false ? "false" : "-";
const boolTone = (value, safeWhenFalse = false) => {
  if (value === true) return safeWhenFalse ? "yellow" : "green";
  if (value === false) return safeWhenFalse ? "green" : "yellow";
  return "gray";
};
function SafetyMetric({ label, value, tone = "green" }) {
  return <div><span>{label}</span><strong>{val(value)}</strong>{tone && <Badge tone={tone}>{val(value)}</Badge>}</div>;
}
function PaperAutomationStatus({
  progress,
  schedulerStatus,
  lockStatus,
}) {
  const lock = lockStatus || schedulerStatus?.lock || {};
  const schedulerEnabled = schedulerStatus?.enabled;
  const schedulerRunning = schedulerStatus?.scheduler_running;
  const autoEnabled = schedulerStatus?.automatic_updates_enabled;
  const schedulerWarnings = [
    schedulerStatus?.unsafe_warning,
    schedulerStatus?.emergency_warning && schedulerStatus?.emergency_warning !== schedulerStatus?.unsafe_warning
      ? schedulerStatus.emergency_warning
      : null,
  ].filter(Boolean);
  const unsafeReasons = Array.isArray(schedulerStatus?.unsafe_reasons) ? schedulerStatus.unsafe_reasons : [];
  return <Card title="Paper Automation Status" eyebrow="read-only scheduler">
    {schedulerWarnings.length ? <div className="safetyWarningList">{schedulerWarnings.map((warning) => <div key={warning}>{warning}</div>)}</div> : null}
    {unsafeReasons.length ? <div className="safetyWarningList">{unsafeReasons.map((reason) => <div key={reason}>Unsafe scheduler config: {reason}</div>)}</div> : null}
    <div className="safetyGrid">
      <SafetyMetric label="scheduler enabled" value={boolLabel(schedulerEnabled)} tone={boolTone(schedulerEnabled, true)} />
      <SafetyMetric label="scheduler_running" value={boolLabel(schedulerRunning)} tone={boolTone(schedulerRunning, true)} />
      <SafetyMetric label="automatic_updates_enabled" value={boolLabel(autoEnabled)} tone={boolTone(autoEnabled, true)} />
      <SafetyMetric label="recurring_loop_enabled" value={boolLabel(schedulerStatus?.recurring_loop_enabled)} tone={boolTone(schedulerStatus?.recurring_loop_enabled, true)} />
      <SafetyMetric label="mode" value={schedulerStatus?.mode} tone="gray" />
      <SafetyMetric label="dry_run_only" value={boolLabel(schedulerStatus?.dry_run_only)} tone={boolTone(schedulerStatus?.dry_run_only)} />
      <SafetyMetric label="allow_real_writes" value={boolLabel(schedulerStatus?.allow_real_writes)} tone={boolTone(schedulerStatus?.allow_real_writes, true)} />
      <SafetyMetric label="after_market_close_only" value={boolLabel(schedulerStatus?.after_market_close_only)} tone={boolTone(schedulerStatus?.after_market_close_only)} />
      <SafetyMetric label="dry_run_first" value={boolLabel(schedulerStatus?.dry_run_first)} tone={boolTone(schedulerStatus?.dry_run_first)} />
      <SafetyMetric label="max_trades" value={schedulerStatus?.max_trades} tone="yellow" />
      <SafetyMetric label="max_writes" value={schedulerStatus?.max_writes} tone={Number(schedulerStatus?.max_writes) <= 1 ? "green" : "yellow"} />
      <SafetyMetric label="next_run_at" value={schedulerStatus?.next_run_at || "disabled"} tone={schedulerStatus?.next_run_at ? "yellow" : "green"} />
      <SafetyMetric label="last scheduled run ID" value={schedulerStatus?.last_scheduled_run_id || "-"} tone="gray" />
      <SafetyMetric label="last scheduled status" value={schedulerStatus?.last_scheduled_run_status || "-"} tone={statusTone(schedulerStatus?.last_scheduled_run_status)} />
      <SafetyMetric label="scheduler block reason" value={schedulerStatus?.block_reason || schedulerStatus?.last_block_reason || "-"} tone={schedulerStatus?.blocked ? "red" : schedulerStatus?.last_block_reason ? "yellow" : "green"} />
      <SafetyMetric label="latest run ID" value={schedulerStatus?.last_run_id || progress?.run_id || "-"} tone="gray" />
      <SafetyMetric label="latest run status" value={schedulerStatus?.last_run_status || progress?.status || "-"} tone={statusTone(schedulerStatus?.last_run_status || progress?.status)} />
      <SafetyMetric label="proposed_write_count" value={progress?.proposed_write_count ?? 0} tone={Number(progress?.proposed_write_count || 0) <= 1 ? "green" : "yellow"} />
      <SafetyMetric label="updated_count" value={progress?.updated_count ?? 0} tone={Number(progress?.updated_count || 0) === 0 ? "green" : "yellow"} />
      <SafetyMetric label="errors_count" value={progress?.errors_count ?? 0} tone={Number(progress?.errors_count || 0) === 0 ? "green" : "red"} />
      <SafetyMetric label="blocked / reason" value={`${boolLabel(progress?.blocked)} ${progress?.block_reason || ""}`.trim()} tone={progress?.blocked ? "yellow" : "green"} />
      <SafetyMetric label="lock status" value={lock?.status || "-"} tone={lock?.held ? "yellow" : "green"} />
      <SafetyMetric label="lock held" value={boolLabel(lock?.held)} tone={boolTone(lock?.held, true)} />
      <SafetyMetric label="paper_only" value={boolLabel(schedulerStatus?.paper_only ?? progress?.paper_only)} tone="green" />
      <SafetyMetric label="live_trading" value={boolLabel(schedulerStatus?.live_trading ?? progress?.live_trading)} tone={boolTone(schedulerStatus?.live_trading ?? progress?.live_trading, true)} />
      <SafetyMetric label="broker_orders" value={boolLabel(schedulerStatus?.broker_orders ?? progress?.broker_orders)} tone={boolTone(schedulerStatus?.broker_orders ?? progress?.broker_orders, true)} />
    </div>
  </Card>;
}
function PaperUpdateRunHistory({ runs = [] }) {
  const safeRuns = Array.isArray(runs) ? runs.slice(0, 10) : [];
  const columns = ["run_id", "started_at", "finished_at", "mode", "status", "processed", "proposed_write_count", "updated_count", "errors_count", "blocked", "block_reason"];
  return <Card title="Recent Paper Update Runs" eyebrow="read-only history">
    <div className="tableShell results-table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>
      {safeRuns.length ? safeRuns.map((run, index) => <tr key={`${run?.run_id || index}-${index}`}>{columns.map((column) => {
        const value = column === "blocked" ? boolLabel(run?.[column]) : run?.[column];
        if (column === "status") return <td key={column}><Badge tone={statusTone(value)}>{val(value)}</Badge></td>;
        if (column === "blocked") return <td key={column}><Badge tone={run?.blocked ? "yellow" : "green"}>{value}</Badge></td>;
        return <td key={column}>{val(value)}</td>;
      })}</tr>) : <tr><td colSpan={columns.length}>No paper update runs logged yet.</td></tr>}
    </tbody></table></div>
  </Card>;
}

const summaryBreakdownRows = (values = {}) => Object.entries(values || {}).map(([label, count]) => ({ label, count }));

function AiDatasetSummary({ summary, snapshots, outcomePreview, collectionStatus, filters, onFiltersChange, onRefresh, loading, errors = [] }) {
  const readyForModelTraining = summary?.ready_for_model_training === true;
  const readinessReasons = Array.isArray(summary?.readiness_reason) ? summary.readiness_reason : [];
  const checklist = aiDataCollectionChecklist(summary, outcomePreview);
  return <Card title="AI Dataset Summary" eyebrow="read-only dataset tracking">
    <div className="warningText">{readyForModelTraining ? "Dataset readiness checks passed. No AI model is running." : "AI model training is not ready yet."}</div>
    {errors.length ? <div className="datasetTrackingReminder">Some dataset panels could not refresh: {errors.map((item) => item.endpoint).join(", ")}.</div> : null}
    {readinessReasons.length ? <ul className="readinessReasonList">{readinessReasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
    <div className="datasetTrackingReminder">This dashboard is for dataset tracking only. It does not generate predictions or trade recommendations.</div>
    <div className="formGrid aiDatasetFilters">
      <label>Strategy type<select value={filters.strategyType} onChange={(event) => onFiltersChange({ ...filters, strategyType: event.target.value })}><option value="">All strategies</option><option value="momentum">Momentum</option><option value="swing">Swing</option></select></label>
      <label>Timeframe<input value={filters.timeframe} onChange={(event) => onFiltersChange({ ...filters, timeframe: event.target.value })} placeholder="All timeframes" /></label>
      <ActionButton onClick={onRefresh} disabled={loading}>Refresh Dataset Summary</ActionButton>
    </div>
    <div className="statsGrid compact">
      <StatCard label="Ready for Model Training" value={readyForModelTraining ? "YES" : "NO"} tone={readyForModelTraining ? "green" : "yellow"} />
      <StatCard label="Minimum Labels for Training" value={summary?.minimum_labels_for_training ?? "--"} />
      <StatCard label="Total Snapshots" value={summary?.total_snapshots ?? "--"} />
      <StatCard label="Labeled Snapshots" value={summary?.labeled_count ?? summary?.labeled_snapshots ?? "--"} />
      <StatCard label="Unlabeled Snapshots" value={summary?.unlabeled_count ?? summary?.unlabeled_snapshots ?? "--"} tone="yellow" />
      <StatCard label="Win Labels" value={summary?.win_count ?? "--"} />
      <StatCard label="Loss Labels" value={summary?.loss_count ?? "--"} tone="yellow" />
      <StatCard label="Breakeven Labels" value={summary?.breakeven_count ?? "--"} />
      <StatCard label="Missing Source Mode" value={summary?.missing_source_mode_count ?? "--"} tone="yellow" />
      <StatCard label="Missing Data Completeness" value={summary?.missing_data_completeness_count ?? "--"} tone="yellow" />
      <StatCard label="Average Rule Score" value={summary?.average_rule_score ?? "--"} />
      <StatCard label="Average Risk Reward" value={summary?.average_risk_reward ?? "--"} />
      <StatCard label="Earliest Snapshot" value={summary?.earliest_snapshot_time ?? "--"} />
      <StatCard label="Latest Snapshot" value={summary?.latest_snapshot_time ?? "--"} />
    </div>
    <div className="threeGrid aiDatasetBreakdowns">
      <Card title="By Strategy Type"><MiniTable rows={summaryBreakdownRows(summary?.by_strategy_type)} columns={["label", "count"]} /></Card>
      <Card title="By Timeframe"><MiniTable rows={summaryBreakdownRows(summary?.by_timeframe)} columns={["label", "count"]} /></Card>
      <Card title="By Result Label"><MiniTable rows={summaryBreakdownRows(summary?.by_result_label)} columns={["label", "count"]} /></Card>
    </div>
    <div className="twoGrid aiDatasetBreakdowns">
      <Card title="By Source Mode"><MiniTable rows={summaryBreakdownRows(summary?.source_mode_distribution)} columns={["label", "count"]} /></Card>
      <Card title="By Data Completeness"><MiniTable rows={summaryBreakdownRows(summary?.data_completeness_distribution)} columns={["label", "count"]} /></Card>
    </div>
    <Card title="Paper Data Collection Status" eyebrow="read-only coverage report">
      <div className="warningText">{collectionStatus?.ai_model_training_blocked === false ? "AI model training readiness checks passed." : "AI model training is still blocked."}</div>
      <div className="statsGrid compact aiDatasetBreakdowns">
        <StatCard label="Total Paper Trades" value={collectionStatus?.total_paper_trades ?? "--"} />
        <StatCard label="Waiting Paper Trades" value={collectionStatus?.waiting_paper_trades ?? "--"} />
        <StatCard label="Open Paper Trades" value={collectionStatus?.open_paper_trades ?? "--"} />
        <StatCard label="Terminal Paper Trades" value={collectionStatus?.terminal_paper_trades ?? "--"} />
        <StatCard label="Terminal Missing AI Snapshot" value={collectionStatus?.terminal_trades_without_ai_snapshot_count ?? "--"} tone="yellow" />
        <StatCard label="Labeled AI Snapshots" value={collectionStatus?.labeled_ai_snapshots ?? "--"} />
        <StatCard label="Unlabeled AI Snapshots" value={collectionStatus?.unlabeled_ai_snapshots ?? "--"} tone="yellow" />
        <StatCard label="Outcome Attach Eligible" value={collectionStatus?.outcome_attach_eligible_count ?? "--"} />
        <StatCard label="Labels Remaining Before Training" value={collectionStatus?.labels_remaining_before_training ?? "--"} tone="yellow" />
        <StatCard label="AI Model Training Blocked" value={collectionStatus?.ai_model_training_blocked === false ? "NO" : "YES"} tone={collectionStatus?.ai_model_training_blocked === false ? "green" : "yellow"} />
      </div>
      <div className="datasetTrackingReminder">Terminal trades missing AI snapshots: {collectionStatus?.terminal_trades_without_ai_snapshot_symbols?.join(", ") || "none"}</div>
    </Card>
    <Card title="AI Data Collection Checklist" eyebrow="read-only readiness checklist">
      <div className="warningText">{checklist.readyForModelTraining ? "Dataset readiness checks passed. Keep reviewing data quality." : "Keep collecting paper trades."}</div>
      <ul className="readinessReasonList">
        <li>Attach outcomes only after trades are closed.</li>
        <li>Do not train a model until readiness becomes true.</li>
        <li>This is dataset tracking only, not trade advice.</li>
      </ul>
      <div className="statsGrid compact aiDatasetBreakdowns">
        <StatCard label="Minimum Labels Needed" value={checklist.minimumLabels} />
        <StatCard label="Current Labeled Count" value={checklist.labeledCount} />
        <StatCard label="Labels Remaining" value={checklist.labelsRemaining} tone="yellow" />
        <StatCard label="At Least Two Label Classes" value={checklist.hasTwoLabelClasses ? "YES" : "NO"} tone={checklist.hasTwoLabelClasses ? "green" : "yellow"} />
        <StatCard label="Win Count" value={checklist.winCount} />
        <StatCard label="Loss Count" value={checklist.lossCount} tone="yellow" />
        <StatCard label="Breakeven Count" value={checklist.breakevenCount} />
        <StatCard label="Unlabeled / Open Snapshots" value={checklist.unlabeledCount} tone="yellow" />
        <StatCard label="Outcome Preview Eligible" value={checklist.eligibleAttachCount} />
        <StatCard label="Missing Metadata Count" value={checklist.missingMetadataCount} tone="yellow" />
        <StatCard label="Ready for Model Training" value={checklist.readyForModelTraining ? "YES" : "NO"} tone={checklist.readyForModelTraining ? "green" : "yellow"} />
      </div>
    </Card>
    <Card title="AI Feature Snapshots" eyebrow="read-only snapshot table" className="aiSnapshotTable">
      <div className="datasetTrackingReminder">This table is for dataset tracking only. It does not generate predictions or trade recommendations.</div>
      <MiniTable rows={aiSnapshotDisplayRows(snapshots)} columns={["Symbol", "Strategy", "Source", "Completeness", "Label", "Trade status", "Outcome attached?"]} />
    </Card>
    <Card title="Outcome Attach Preview" eyebrow="read-only dry-run preview" className="aiSnapshotTable">
      <div className="warningText">This is a dry-run preview only. It does not attach labels or modify MongoDB.</div>
      <div className="statsGrid compact aiDatasetBreakdowns">
        <StatCard label="Eligible Attachments" value={outcomePreview?.eligible_attach_count ?? "--"} />
        <StatCard label="Open Skipped" value={outcomePreview?.skipped_open_count ?? "--"} tone="yellow" />
        <StatCard label="Already Labeled" value={outcomePreview?.skipped_already_labeled_count ?? "--"} />
        <StatCard label="Missing Trade" value={outcomePreview?.skipped_missing_trade_count ?? "--"} tone="yellow" />
      </div>
      <div className="twoGrid aiDatasetBreakdowns">
        <Card title="Eligible Snapshots"><MiniTable rows={aiOutcomeEligibleRows(outcomePreview?.eligible_snapshots)} columns={["Symbol", "Proposed label", "Trade status"]} /></Card>
        <Card title="Skipped Snapshots"><MiniTable rows={aiOutcomeSkippedRows(outcomePreview?.skipped_snapshots)} columns={["Symbol", "Reason", "Trade status", "Label"]} /></Card>
      </div>
    </Card>
  </Card>;
}

function DashboardHealthPanel({ health, tradingViewStatus, schedulerStatus }) {
  const tvBadge = tradingViewBadge(tradingViewStatus);
  const schedulerBlocked = schedulerStatus?.blocked || schedulerStatus?.unsafe_warning || schedulerStatus?.last_block_reason;
  return <Card title="Scheduler and TradingView Health" eyebrow="automatic status">
    <div className="safetyGrid">
      <div><span>Backend</span><strong>{health?.status || "unknown"}</strong><Badge tone={health?.online ? "green" : "red"}>{health?.online ? "online" : "offline"}</Badge></div>
      <div><span>TradingView</span><strong>{tvBadge.label.replace("TradingView ", "")}</strong><Badge tone={tvBadge.tone}>{tradingViewStatus?.connected ? "connected" : "not connected"}</Badge></div>
      <div><span>TV Worker</span><strong>{tradingViewStatus?.worker_running ? "Busy" : "Idle"}</strong><Badge tone={tradingViewStatus?.worker_running ? "yellow" : "green"}>{tradingViewStatus?.worker_running ? "running" : "clear"}</Badge></div>
      <div><span>Scheduler</span><strong>{schedulerStatus?.scheduler_running ? "Running" : "Idle"}</strong><Badge tone={schedulerBlocked ? "yellow" : "green"}>{schedulerBlocked ? "blocked" : "safe"}</Badge></div>
      <div><span>Next Run</span><strong>{schedulerStatus?.next_run_at || "disabled"}</strong><Badge tone={schedulerStatus?.next_run_at ? "yellow" : "gray"}>{schedulerStatus?.mode || "dry-run"}</Badge></div>
      <div><span>Latest Run</span><strong>{schedulerStatus?.last_run_status || "-"}</strong><Badge tone={statusTone(schedulerStatus?.last_run_status)}>{schedulerStatus?.last_run_id || "-"}</Badge></div>
    </div>
  </Card>;
}

function DashboardPortfolio({ data }) {
  const statusCounts = data?.status_counts || {};
  const numberField = (key) => Number.isFinite(Number(data?.[key])) ? Number(data[key]) : 0;
  const countField = (key) => Number.isFinite(Number(statusCounts?.[key])) ? Number(statusCounts[key]) : 0;
  const rowNumber = (row, key) => Number.isFinite(Number(row?.[key])) ? Number(row[key]) : 0;
  const strategyRows = Array.isArray(data?.strategy_comparison_rows) ? data.strategy_comparison_rows : [];
  const monthlyRows = Array.isArray(data?.monthly_pnl_rows) ? data.monthly_pnl_rows : [];
  const completedRows = Array.isArray(data?.recent_completed_trades) ? data.recent_completed_trades : [];
  const exposureRows = Array.isArray(data?.open_position_exposure) ? data.open_position_exposure : [];
  const comparisonRows = strategyRows.map((row) => ({
    Strategy: row.strategy || "",
    Trades: rowNumber(row, "total_trades"),
    "Win %": pct(rowNumber(row, "win_rate")),
    "Profit Factor": rowNumber(row, "profit_factor"),
    "Average RR": rowNumber(row, "average_rr"),
  }));
  const recentRows = completedRows.map((row) => ({
    Symbol: row.symbol || "",
    Strategy: row.strategy || "",
    "Exit Date": row.exit_date || "",
    Reason: row.exit_reason || "",
    "Original Margin": money(rowNumber(row, "initial_margin_reserved")),
    "Total Released": money(rowNumber(row, "margin_released_total")),
    "Realized P&L": money(rowNumber(row, "realized_pnl")),
    "Cash Returned": money(rowNumber(row, "cash_returned_on_final_exit")),
  }));
  const openRows = exposureRows.map((row) => ({
    Symbol: row.symbol || "",
    Strategy: row.strategy || "",
    Status: row.status || "",
    "Original Qty": rowNumber(row, "original_quantity"),
    "Remaining Qty": rowNumber(row, "quantity_remaining"),
    "Original Margin": money(rowNumber(row, "initial_margin_reserved")),
    "Remaining Margin": money(rowNumber(row, "margin_remaining")),
    "Margin Released": money(rowNumber(row, "margin_released_total")),
    "Realized P&L": money(rowNumber(row, "realized_pnl")),
    "Unrealized P&L": money(rowNumber(row, "unrealized_pnl")),
  }));
  return <>
    <div className="heroGrid">
      <StatCard label="Starting Virtual Capital" value={money(numberField("starting_virtual_capital"))} />
      <StatCard label="Settled Balance" value={money(numberField("settled_balance"))} />
      <StatCard label="Available Cash" value={money(numberField("available_cash"))} />
      <StatCard label="Total Equity" value={money(numberField("total_equity"))} />
    </div>
    <div className="statsGrid">
      <StatCard label="Active Trades (Full / Partial)" value={(() => {
        const raw = data?.active_partial_trade_count;
        if (typeof raw === "string" && raw.trim()) return raw.trim();
        if (typeof raw === "number" && Number.isFinite(raw)) return String(raw);
        return "0 / 0";
      })()} />
      <StatCard label="Waiting" value={countField("waiting")} />
      <StatCard label="Completed" value={countField("completed")} />
      <StatCard label="Win Rate" value={pct(numberField("win_rate_percent"))} tone="yellow" />
      <StatCard label="Profit Factor" value={fmt(numberField("profit_factor"))} tone="green" />
    </div>
    <div className="twoColGrid">
      <Card title="Capital Flow" eyebrow="ACCOUNTING">
        <div style={{ padding: "12px", fontSize: "14px", lineHeight: "1.8" }}>
          <div style={{ display: "flex", justifyContent: "space-between" }}><span>Starting Virtual Capital</span><strong>{money(numberField("starting_virtual_capital"))}</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between" }}><span>Reserved Margin</span><strong style={{color:"var(--warning)"}}>− {money(numberField("reserved_margin"))}</strong></div>
          <div style={{ display: "flex", justifyContent: "space-between" }}><span>Total Realized P&L</span><strong style={{color:"var(--profit)"}}>+ {money(numberField("total_realized_pnl"))}</strong></div>
          <hr style={{ border: "0", borderTop: "1px solid var(--border-soft)", margin: "12px 0" }} />
          <div style={{ display: "flex", justifyContent: "space-between" }}><span>Available Cash</span><strong>{money(numberField("available_cash"))}</strong></div>
          <div style={{ marginTop: "16px", color: "var(--text-muted)", fontSize: "12px" }}>
            Total Equity = Settled Balance ({money(numberField("settled_balance"))}) + Unrealized P&L ({money(numberField("unrealized_pnl"))}) = {money(numberField("total_equity"))}
          </div>
        </div>
      </Card>
      <Card title="Swing vs Momentum" eyebrow="PERFORMANCE"><MiniTable rows={comparisonRows} columns={["Strategy", "Trades", "Win %", "Profit Factor", "Average RR"]} /></Card>
    </div>
    <div className="twoColGrid">
      <Card title="Recent Completed Trades" eyebrow="COMPLETED TRADES"><MiniTable rows={recentRows} columns={["Symbol", "Strategy", "Exit Date", "Reason", "Original Margin", "Total Released", "Realized P&L", "Cash Returned"]} /></Card>
      <Card title="Open-Position Exposure" eyebrow="ACTIVE POSITIONS">
        <MiniTable rows={openRows} columns={["Symbol", "Strategy", "Status", "Original Qty", "Remaining Qty", "Original Margin", "Remaining Margin", "Margin Released", "Realized P&L", "Unrealized P&L"]} />
      </Card>
    </div>
    
    <details className="debug" style={{ marginTop: "20px" }}>
      <summary>Detailed Metrics (Diagnostic)</summary>
      <div className="statsGrid compact" style={{ marginTop: "16px" }}>
        <StatCard label="Effective Open Exposure" value={money(numberField("effective_open_exposure"))} />
        <StatCard label="Broker Funded Exposure" value={money(numberField("broker_funded_exposure"))} />
        <StatCard label="Active/Partial Trade Count" value={data?.active_partial_trade_count || "0 / 0"} />
        <StatCard label="Total Margin Released" value={money(numberField("total_margin_released"))} />
        <StatCard label="Capital Returned From Latest Exits" value={money(numberField("capital_returned_from_latest_exits"))} />
        <StatCard label="Drawdown" value={pct(numberField("drawdown_percent"))} />
        <StatCard label="Average RR" value={fmt(numberField("average_rr"))} />
      </div>
    </details>
  </>;
}
function Dashboard({
  dashboardEquity,
  health,
  tvRuntimeStatus,
  dashboardErrors,
  paperUpdateScheduler,
  onSummary,
  onDryRun,
  onSaveRun,
  loading,
}) {
  return <div className="pageStack">
    <div className="topHeader">
      <div>
        <h2>Trading Dashboard</h2>
        <p>Paper terminal overview and system performance</p>
      </div>
      <div className="topHeaderRight">
        <Badge tone={health?.online ? "green" : "red"}>API {health?.online ? "ONLINE" : "OFFLINE"}</Badge>
        <Badge tone={tvRuntimeStatus?.worker_running ? "yellow" : tvRuntimeStatus?.connected ? "green" : "red"}>TV {tvRuntimeStatus?.worker_running ? "BUSY" : tvRuntimeStatus?.connected ? "READY" : "OFFLINE"}</Badge>
        <Badge tone={paperUpdateScheduler?.active ? "green" : "gray"}>SYNC {paperUpdateScheduler?.active ? "ON" : "OFF"}</Badge>
      </div>
    </div>
    <div className="pageContent pageStack">
      <div className="buttonRow">
        <ActionButton className="primary" onClick={onSummary} disabled={loading}>Refresh Summary</ActionButton>
        <ActionButton onClick={onDryRun} disabled={loading}>Pipeline Dry Run</ActionButton>
        <ActionButton onClick={onSaveRun} disabled={loading}>Save Paper Run</ActionButton>
      </div>
      {dashboardErrors && dashboardErrors.length > 0 && (
        <div className="errorPanel">
          <strong>Some dashboard panels could not refresh:</strong>
          <ul style={{ margin: "5px 0 0 20px", padding: 0 }}>
            {dashboardErrors.map((err, idx) => (
              <li key={idx}>{err.endpoint}: {err.message}</li>
            ))}
          </ul>
        </div>
      )}
      <DashboardPortfolio data={dashboardEquity} />
    </div>
  </div>;
}
function SummaryCards({ data, fields }) {
  return <div className="statsGrid compact">{fields.map((field) => (
    <StatCard key={field} label={field} value={data?.[field] ?? "--"} tone={field.includes("invalid") || field.includes("overextended") ? "red" : "green"} />
  ))}</div>;
}

const SCAN_SCORE_WARNING = "Market data updated. Please run Score Market Data before loading Swing/Momentum candidates.";
const SCORE_REFRESH_MESSAGE = "Score Market Data completed. Now load Swing/Momentum candidates.";
const MOMENTUM_SCORE_STALE_MESSAGE = "Market data is newer than scored candidates. Rerun Score Market Data before Momentum TV Confirm.";
const TRADINGVIEW_ERROR_HINT = "If this is a TradingView connection issue, make sure TradingView Desktop/debug access and the backend on 127.0.0.1:8011 are running.";
const DASHBOARD_REFRESH_MS = 30000;
const GLOBAL_HEALTH_REFRESH_MS = 30000;
const BATCH_TV_RUNTIME_REFRESH_MS = 2000;

function sanitizeErrorMessage(str) {
  if (!str) return "";
  const cleanStr = String(str);
  if (/<[a-z][\s\S]*>/i.test(cleanStr) || cleanStr.includes("<!DOCTYPE") || cleanStr.includes("<html")) {
    return "[RAW_HTML_RESPONSE]";
  }
  let clean = cleanStr.replace(/[A-Za-z]:\\[^:\n]+/g, "[PATH]");
  clean = clean.replace(/\/[a-zA-Z0-9_\-\.\/]+/g, (match) => {
    if (match.includes("/") && match.split("/").length > 2) {
      return "[PATH]";
    }
    return match;
  });
  clean = clean.replace(/\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/g, "[IP]");
  clean = clean.replace(/mongodb(\+srv)?:\/\/[^\s"'`>]+/gi, "[MONGO_URI]");
  clean = clean.replace(/(?:key|secret|token|password|credential|auth)[^\s"'`>:=]*[:=]\s*[^\s"'`>]+/gi, (match) => {
    const parts = match.split(/[:=]/);
    return `${parts[0]}: [REDACTED]`;
  });
  if (clean.length > 1000) {
    clean = clean.slice(0, 1000) + "... [TRUNCATED]";
  }
  return clean;
}

function formatActionError(err, actionName = "request") {
  if (isRequestCancellation(err)) return "";
  const status = err?.status ? `HTTP ${err.status}` : "HTTP status unavailable";
  const rawMessage = err?.message || String(err);
  const message = sanitizeErrorMessage(rawMessage);
  const safeBodyText = err?.responseBody
    ? [err.responseBody.message, err.responseBody.detail, err.responseBody.error]
        .filter((x) => typeof x === "string" && x)
        .map(sanitizeErrorMessage)
        .join("; ")
    : "";
  const actionText = String(actionName || "").toLowerCase();
  const shouldShowTvHint = actionText.includes("tv") || /tradingview|debug|connection|fetch|timeout|network/i.test(`${message} ${safeBodyText}`);
  return `${status}\nMessage: ${message}${safeBodyText ? `\nDetail: ${safeBodyText}` : ""}${shouldShowTvHint ? `\nHint: ${TRADINGVIEW_ERROR_HINT}` : ""}`;
}

function tvRows(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.rows)) return data.rows;
  if (Array.isArray(data?.data?.rows)) return data.data.rows;
  if (Array.isArray(data?.data)) return data.data;
  return arr(data, ["results", "returned_rows", "rows"]);
}

function normalizeSavedTvResponse(data, fallbackLimit = 20) {
  const rows = tvRows(data);
  const base = data && !Array.isArray(data) ? data : {};
  return {
    ...base,
    index_name: base.index_name || "BROAD_MARKET_750",
    limit: base.limit ?? fallbackLimit,
    saved_rows_count: base.saved_rows_count ?? rows.length,
    rows_count: base.rows_count ?? rows.length,
    rows,
  };
}

function normalizeSearchSymbol(input) {
  const raw = String(input || "").trim().toUpperCase();
  if (!raw || raw === "-" || raw === "UNDEFINED" || raw === "NULL") return { exchange: "NSE", symbol: "", tradingview_symbol: "" };
  if (raw.includes(":")) {
    const [exchange, ...parts] = raw.split(":");
    const symbol = parts.join(":").trim();
    const cleanExchange = exchange.trim() || "NSE";
    if (!symbol || symbol === "-" || symbol === "UNDEFINED" || symbol === "NULL") {
      return { exchange: cleanExchange, symbol: "", tradingview_symbol: "" };
    }
    return { exchange: cleanExchange, symbol, tradingview_symbol: symbol ? `${cleanExchange}:${symbol}` : "" };
  }
  return { exchange: "NSE", symbol: raw, tradingview_symbol: `NSE:${raw}` };
}

const SUPPORTED_TIMEFRAMES = new Set(["1m", "3m", "5m", "15m", "30m", "45m", "1h", "2h", "3h", "4h", "1D", "1W", "1M"]);

function normalizeTimeframe(p) {
  const clean = String(p || "").trim();
  if (!clean) return "";
  if (/^\d+[mh]$/i.test(clean)) {
    return clean.toLowerCase();
  }
  return clean.toUpperCase();
}

function validateTimeframes(input) {
  const parts = String(input || "").split(",").map((p) => p.trim());
  if (!parts.length || (parts.length === 1 && !parts[0])) return false;
  return parts.every((p) => SUPPORTED_TIMEFRAMES.has(normalizeTimeframe(p)));
}

function firstRow(data) {
  return data?.row || tvRows(data)[0] || {};
}

const SWING_MTF_TIMEFRAMES = "1W,1D,4H,1H";
const MOMENTUM_MTF_TIMEFRAMES = "1D,4H,1H";
const SWING_TV_BATCH_SIZE = 1;
const MOMENTUM_TV_BATCH_SIZE = 1;
const TV_BUSY_MAX_RETRIES = 3;
const TV_BUSY_BASE_DELAY_MS = 2000;
const isTvManagerBusy = (err) => err?.responseBody?.code === "TV_MANAGER_BUSY" || err?.responseBody?.retryable === true;
const delay = (ms) => new Promise((resolve) => globalThis.setTimeout(resolve, ms));
const SWING_CONFIRMED_STATUSES = new Set(["CONFIRMED_SIGNAL"]);
const SWING_WAIT_WATCH_STATUSES = new Set(["WAIT_FOR_RETEST", "WATCH_FOR_PULLBACK", "WATCH_FOR_BREAKOUT"]);
const MOMENTUM_CONFIRMED_STATUSES = new Set(["MOMENTUM_CONFIRMED"]);
const MOMENTUM_WAIT_WATCH_STATUSES = new Set(["WAIT_FOR_PULLBACK", "WATCH_FOR_PULLBACK", "WATCH_FOR_BREAKOUT"]);
const FAILED_STATUSES = new Set(["REJECTED", "TECHNICAL_FAILED"]);

function flattenMtfRows(rows) {
  return (Array.isArray(rows) ? rows : []).map((row) => ({
    ...row,
    weekly_bias: row?.mtf_summary?.weekly_bias ?? "-",
    daily_setup: row?.mtf_summary?.daily_setup ?? "-",
    four_hour_confirmation: row?.mtf_summary?.four_hour_confirmation ?? "-",
    one_hour_entry: row?.mtf_summary?.one_hour_entry ?? "-",
    candles_1W: row?.candles_by_timeframe?.["1W"] ?? row?.timeframe_analysis?.["1W"]?.candles_count ?? "-",
    candles_1D: row?.candles_by_timeframe?.["1D"] ?? row?.timeframe_analysis?.["1D"]?.candles_count ?? row?.candles_count ?? "-",
    candles_4H: row?.candles_by_timeframe?.["4H"] ?? row?.timeframe_analysis?.["4H"]?.candles_count ?? "-",
    candles_1H: row?.candles_by_timeframe?.["1H"] ?? row?.timeframe_analysis?.["1H"]?.candles_count ?? "-",
  }));
}

function flattenMomentumMtfRows(rows) {
  return (Array.isArray(rows) ? rows : []).map((row) => {
    const daily = row?.timeframe_analysis?.["1D"] || {};
    const oneHour = row?.timeframe_analysis?.["1H"] || {};
    return {
      ...row,
      weekly_filter: row?.weekly_bias ?? "-",
      candles_1W: row?.candles_by_timeframe?.["1W"] ?? row?.timeframe_analysis?.["1W"]?.candles_count ?? "-",
      candles_1D: row?.candles_by_timeframe?.["1D"] ?? daily?.candles_count ?? "-",
      candles_4H: row?.candles_by_timeframe?.["4H"] ?? row?.timeframe_analysis?.["4H"]?.candles_count ?? "-",
      candles_1H: row?.candles_by_timeframe?.["1H"] ?? oneHour?.candles_count ?? "-",
      fake_breakout_risk: row?.risk_summary?.fake_breakout_risk ?? "-",
      overextended_risk: row?.risk_summary?.overextended_risk ?? "-",
      volume_confirmation: daily?.strong_or_improving_volume === true ? "YES" : daily?.strong_or_improving_volume === false ? "NO" : "-",
      entry_quality: row?.one_hour_entry ?? "-",
    };
  });
}

function MtfSummaryCards({ row }) {
  const mtf = row?.mtf_summary || {};
  const conflicts = Array.isArray(mtf.conflicting_timeframes) ? mtf.conflicting_timeframes.join(", ") : "";
  const cards = [
    { label: "Weekly Bias", value: mtf.weekly_bias ?? "-", tone: statusTone(mtf.weekly_bias) },
    { label: "Daily Setup", value: mtf.daily_setup ?? "-", tone: statusTone(mtf.daily_setup) },
    { label: "4H Confirmation", value: mtf.four_hour_confirmation ?? "-", tone: statusTone(mtf.four_hour_confirmation) },
    { label: "1H Entry", value: mtf.one_hour_entry ?? "-", tone: statusTone(mtf.one_hour_entry) },
    { label: "All Timeframes Aligned", value: mtf.all_timeframes_aligned === true ? "YES" : mtf.all_timeframes_aligned === false ? "NO" : "-", tone: mtf.all_timeframes_aligned ? "green" : "yellow" },
    { label: "Conflicting Timeframes", value: conflicts || "None", tone: conflicts ? "red" : "green" },
  ];
  return <div className="mtfSummaryGrid">{cards.map((item) => (
    <div className={`mtfSummaryCard accent-${item.tone}`} key={item.label}>
      <span>{item.label}</span>
      <strong>{item.value}</strong>
    </div>
  ))}</div>;
}

function CandleCountCards({ row }) {
  const counts = row?.candles_by_timeframe || {};
  return <div className="statsGrid compact">
    {["1W", "1D", "4H", "1H"].map((timeframe) => (
      <StatCard key={timeframe} label={`${timeframe} candles`} value={counts?.[timeframe] ?? row?.timeframe_analysis?.[timeframe]?.candles_count ?? (timeframe === "1D" ? row?.candles_count : "--")} tone="green" />
    ))}
  </div>;
}

function CandidateWatchlistGrid({ rows, selectedRow, onSelect, mode }) {
  const isMomentum = mode === "momentum";
  const scoreField = isMomentum ? "momentum_score" : "score";
  const label = isMomentum ? "MOMENTUM WATCHLIST" : "SWING WATCHLIST";
  const strongLabel = isMomentum ? "STRONG MOMENTUM WATCHLIST" : "STRONG SWING WATCHLIST";
  const selectedKey = `${selectedRow?.symbol || ""}-${selectedRow?.tradingview_symbol || ""}`;
  return <div className="candidateCardGrid">
    {rows.map((row, index) => {
      const rowKey = `${row?.symbol || ""}-${row?.tradingview_symbol || ""}`;
      const score = Number(row?.[scoreField] || 0);
      const scoreText = Number.isFinite(score) ? Math.round(score) : 0;
      const statusLabel = score >= 90 ? strongLabel : label;
      return <button
        className={`watchlistCard ${selectedKey === rowKey ? "selectedWatchlistCard" : ""}`}
        key={`${rowKey}-${index}`}
        type="button"
        onClick={() => onSelect(row)}
      >
        <strong className="watchlistSymbol">{val(row?.symbol)}</strong>
        <span className="watchlistPill">{statusLabel}</span>
        <span className="watchlistScore">{scoreText}/100</span>
      </button>;
    })}
  </div>;
}

function rowSearchSymbol(row) {
  if (!row) return "";
  const tv = String(row?.requested_tradingview_symbol || row?.tradingview_symbol || "").trim().toUpperCase();
  const symbol = String(row?.symbol || row?.canonical_symbol || "").trim().toUpperCase();
  const exchange = String(row?.exchange || "NSE").trim().toUpperCase();
  if (tv) return tv;
  if (!symbol || symbol === "-" || symbol === "UNDEFINED" || symbol === "NULL") return "";
  return `${exchange}:${symbol}`;
}

function savedStatus(row) {
  return String(row?.tv_status || row?.status || row?.final_status || "").toUpperCase();
}

function SavedResultCard({ row, mode, onOpenStock }) {
  const failed = failedTimeframes(row);
  const isMomentum = mode === "momentum";
  const shortReason = String(row?.reason || "-").split(";")[0].slice(0, 110);
  const openStock = () => onOpenStock?.(row);
  return <div className="savedResultCard" role="button" tabIndex={0} onClick={openStock} onKeyDown={(event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openStock();
    }
  }}>
    <div className="savedResultTop">
      <strong>{val(row?.symbol || row?.canonical_symbol || row?.tradingview_symbol)}</strong>
      <Badge tone={statusTone(savedStatus(row))}>{val(savedStatus(row))}</Badge>
      {row?.trade_quality_grade && <Badge tone={qualityTone(row.trade_quality_grade)}>{qualityLabel(row.trade_quality_grade)}</Badge>}
    </div>
    <div className="savedResultMeta">
      <span>Confirmed: {confirmationTimestampLabel(row, mode)}</span>
      <span>{fmt(row?.confidence_score ?? row?.score ?? row?.momentum_score)} confidence</span>
      <span>{shortReason}</span>
      {isMomentum && row?.trap_status && <span>trap: {row.trap_status}</span>}
      {failed.length > 0 && <span>failed: {failed.join(", ")}</span>}
    </div>
    {row?.weekly_history_warning && <p className="savedResultWarning">Weekly data insufficient. Daily used as higher-timeframe backup.</p>}
  </div>;
}

function TvResultSummaryPanel({ title, rows, mode, loaded, candidateCount: rawCandidateCount = 0, onOpenStock }) {
  const [openSection, setOpenSection] = useState(null);
  const confirmedStatuses = mode === "momentum" ? MOMENTUM_CONFIRMED_STATUSES : SWING_CONFIRMED_STATUSES;
  const waitWatchStatuses = mode === "momentum" ? MOMENTUM_WAIT_WATCH_STATUSES : SWING_WAIT_WATCH_STATUSES;
  const safeRows = Array.isArray(rows) ? rows : [];
  const candidateCount = countValue(rawCandidateCount);
  const confirmedRows = safeRows.filter((row) => confirmedStatuses.has(savedStatus(row)));
  const waitWatchRows = safeRows.filter((row) => waitWatchStatuses.has(savedStatus(row)));
  const confirmedWatchRows = sortByTradeQuality([...confirmedRows, ...waitWatchRows]);
  const rejectedRows = sortByTradeQuality(safeRows.filter((row) => savedStatus(row) === "REJECTED"));
  const technicalFailedRows = sortByTradeQuality(safeRows.filter((row) => savedStatus(row) === "TECHNICAL_FAILED"));

  const activeRows = openSection === "confirmed" ? confirmedWatchRows :
                     openSection === "rejected" ? rejectedRows :
                     openSection === "failed" ? technicalFailedRows : [];
  if (!loaded) {
    return <Card title={title} eyebrow={mode === "momentum" ? "momentum_tv_confirmations" : "swing_tv_confirmations"}>
      <p className="muted">No saved TV results loaded. Run TV Confirm first or click Load Saved TV Results.</p>
    </Card>;
  }
  return <Card title={title} eyebrow={mode === "momentum" ? "momentum_tv_confirmations" : "swing_tv_confirmations"}>
    <div className="savedResultSummary">
      <button className={`savedSummaryCard ${openSection === "confirmed" ? "active" : ""}`} type="button" onClick={() => setOpenSection(openSection === "confirmed" ? null : "confirmed")}>
        <span>Confirmed / Watch</span><strong>{confirmedWatchRows.length}</strong>
      </button>
      <button className={`savedSummaryCard ${openSection === "rejected" ? "active" : ""}`} type="button" onClick={() => setOpenSection(openSection === "rejected" ? null : "rejected")}>
        <span>Strategy Rejected</span><strong>{rejectedRows.length}</strong>
      </button>
      <button className={`savedSummaryCard ${openSection === "failed" ? "active" : ""}`} type="button" onClick={() => setOpenSection(openSection === "failed" ? null : "failed")}>
        <span>Technical Failed</span><strong>{technicalFailedRows.length}</strong>
      </button>
    </div>
    <p className="muted">
      {(() => {
        const savedResultCount = safeRows.length;
        const strategyLabel = mode === "momentum" ? "Momentum" : "Swing";
        return <>Only {savedResultCount}/{candidateCount} current {strategyLabel} candidates have saved TV results.<br />(stale or outside-scope saved TV rows ignored.)</>;
      })()}
    </p>
    {safeRows.length === 0 && <p className="muted">No saved TV rows found.</p>}
    {openSection && <div className="savedResultList">
      {activeRows.length ? activeRows.map((row, index) => <SavedResultCard key={`${row?.symbol || row?.tradingview_symbol || openSection}-${index}`} row={row} mode={mode} onOpenStock={onOpenStock} />) : <p className="muted">No {openSection === "confirmed" ? "Confirmed / Watch" : openSection === "rejected" ? "Strategy Rejected" : "Technical Failed"} TV results.</p>}
    </div>}
  </Card>;
}

function candidateSymbols(rows, offset, limit) {
  return (Array.isArray(rows) ? rows : [])
    .slice(offset, offset + limit)
    .map((row) => {
      const symbol = row?.symbol || row?.canonical_symbol || "";
      const requested = row?.tradingview_symbol || (symbol ? `${row?.exchange || "NSE"}:${symbol}` : "");
      if (!symbol && !requested) return null;
      return { symbol, requested_tradingview_symbol: requested };
    })
    .filter(Boolean);
}

function progressSymbolLabel(item) {
  if (typeof item === "string") return item;
  const symbol = item?.symbol || item?.canonical_symbol || "";
  const requested = item?.requested_tradingview_symbol || item?.tradingview_symbol || "";
  return requested ? `${symbol} / ${requested}` : symbol;
}

function SymbolFixWarnings({ rows }) {
  const fixedRows = (Array.isArray(rows) ? rows : []).filter((row) => row?.tradingview_symbol_rebuilt);
  if (!fixedRows.length) return null;
  return <div className="warningText">{fixedRows.map((row) => `TradingView symbol fixed from ${row?.old_tradingview_symbol || row?.original_tradingview_symbol || "missing"} to ${row?.fixed_tradingview_symbol || row?.requested_tradingview_symbol}`).join("; ")}</div>;
}

function WeeklyHistoryNotice({ rows }) {
  const warningRows = (Array.isArray(rows) ? rows : []).filter((row) => row?.weekly_history_warning);
  if (!warningRows.length) return null;
  const symbols = warningRows.map((row) => row?.symbol || row?.tradingview_symbol).filter(Boolean).join(", ");
  return <div className="warningText">Weekly data insufficient. Daily used as higher-timeframe backup.{symbols ? ` ${symbols}` : ""}</div>;
}

function TvDiagnosticWarning({ status }) {
  if (!status) return null;
  if (status.preflight_ready === true) {
    return <div className="tvDiagnosticReady" id="tv-diagnostic-ready">
      <h3>TradingView Ready / Connected</h3>
      <div className="tvDiagnosticReadyInfo">
        <div><strong>Valid Chart Tabs Open:</strong> {status.valid_chart_target_count ?? 0}</div>
        <div><strong>Chart Ready:</strong> Yes</div>
      </div>
    </div>;
  }
  const singleTabPending =
    status.cdp_reachable &&
    (status.valid_chart_target_count === 1) &&
    !status.attached_target_id &&
    !status.manual_attachment_required;
  if (singleTabPending) {
    return <div className="tvDiagnosticPending" id="tv-diagnostic-pending">
      <h3>TradingView Chart Available</h3>
      <p className="pendingMsg">One TradingView chart is available and will attach automatically when a TradingView operation runs.</p>
      <div className="tvDiagnosticReadyInfo">
        <div><strong>CDP Reachable (127.0.0.1:9222):</strong> Yes</div>
        <div><strong>Valid Chart Tabs Open:</strong> 1</div>
        <div><strong>Auto-Attach:</strong> Will attach on next operation</div>
      </div>
    </div>;
  }
  return <div className="actionableWarning" id="tv-diagnostic-warning">
    <h3>TradingView Tab Selection / Attachment Required</h3>
    <p className="warningMsg">{status.preflight_message || "TradingView chart is not ready or attached."}</p>
    <div className="tvDiagnosticInfo">
      <div><strong>CDP Reachable (127.0.0.1:9222):</strong> {status.cdp_reachable ? "Yes" : "No"}</div>
      <div><strong>Valid Chart Tabs Open:</strong> {status.valid_chart_target_count ?? 0}</div>
      <div><strong>Attached Tab ID:</strong> {status.attached_target_id || "None"}</div>
      {status.attached_target && <>
        <div><strong>Attached Tab Title:</strong> {status.attached_target.title || "None"}</div>
        <div><strong>Attached Tab URL:</strong> {status.attached_target.url || "None"}</div>
      </>}
      <div><strong>Chart Ready (activeChart):</strong> {status.attached_target_ready ? "Yes" : "No"}</div>
      {status.last_attachment_error && <div className="errorText"><strong>Last Error:</strong> {status.last_attachment_error}</div>}
    </div>
    <p className="instruction">
      {status.manual_attachment_required
        ? "Please go to the Settings tab, select an active TradingView chart, and click Attach."
        : "Make sure TradingView Desktop is running with a chart tab open."}
    </p>
  </div>;
}

function BatchProgressNotice({ progress }) {
  if (!progress) return null;
  const symbols = Array.isArray(progress.current_batch_symbols) ? progress.current_batch_symbols : [];
  const labels = symbols.map(progressSymbolLabel).filter(Boolean);
  return <div className="noticePanel inlineNotice batchProgressPanel">
    <strong>{progress.process_type}: Batch {progress.current_batch_number} of {progress.total_batches}</strong>
    <p>Stocks {progress.current_batch_range} of {progress.total_requested}. Processed so far: {progress.processed_so_far} / {progress.total_requested}.</p>
    <p>Elapsed time: {progress.elapsed_time}s. Status: TradingView confirmation is running. Please do not touch TradingView.</p>
    {labels.length > 0 && <>
      <p>Currently processing Batch {progress.current_batch_number} of {progress.total_batches}:</p>
      <ul className="batchStockList">{labels.map((label) => <li key={label}>Currently analyzing: {label}</li>)}</ul>
    </>}
  </div>;
}

const SYMBOL_DIAGNOSTIC_FIELDS = [
  "requested_tradingview_symbol",
  "previous_active_symbol",
  "active_symbol_after_set",
  "active_symbol_after_stabilize",
  "symbol_match",
  "symbol_stable_check_passed",
  "stale_previous_symbol_warning",
];

function failedTimeframes(row) {
  const analysis = row?.timeframe_analysis || {};
  const fromAnalysis = Object.keys(analysis).filter((timeframe) => analysis?.[timeframe]?.technical_failed);
  if (fromAnalysis.length) return fromAnalysis;
  const match = String(row?.reason || "").match(/KEY_TIMEFRAME_FAILED:([^;]+)/);
  return match ? match[1].split(",").map((item) => item.trim()).filter(Boolean) : [];
}

function pickFields(row, fields) {
  return fields.reduce((out, field) => ({ ...out, [field]: row?.[field] ?? null }), {});
}

function TechnicalErrorDetails({ rows }) {
  const failedRows = (Array.isArray(rows) ? rows : []).filter((row) => failedTimeframes(row).length);
  if (!failedRows.length) return null;
  return <div className="technicalErrorList">
    {failedRows.map((row, index) => {
      const failed = failedTimeframes(row);
      const failedDebug = failed.reduce((out, timeframe) => ({ ...out, [timeframe]: row?.timeframe_debug?.[timeframe] || {} }), {});
      const explanation = failed.map((timeframe) => `${timeframe} failed because ${failedDebug?.[timeframe]?.error_message || row?.timeframe_analysis?.[timeframe]?.error || row?.reason || "technical diagnostics unavailable"}`).join("; ");
      return <details className="debug technicalErrorDetails" key={`${row?.symbol || index}-technical-error`}>
        <summary>Technical Error Details - {row?.tradingview_symbol || row?.symbol || "Unknown"}</summary>
        <p className="chartMeta">{explanation}</p>
        <pre>{JSON.stringify({
          failed_timeframes: failed,
          reason: row?.reason,
          error: row?.error,
          symbol_diagnostics: pickFields(row, SYMBOL_DIAGNOSTIC_FIELDS),
          timeframe_debug: failedDebug,
        }, null, 2)}</pre>
      </details>;
    })}
  </div>;
}

function tvConfirmSummary(data) {
  const rows = tvRows(data);
  const failed = rows.filter((row) => String(row?.reason || row?.final_status || "").includes("ERROR") || String(row?.reason || row?.final_status || "").includes("FAILED")).length;
  const confirmed = rows.filter((row) => row?.tv_confirmed === true).length;
  return {
    processed: data?.processed ?? rows.length,
    tv_confirmed: data?.confirmed_count ?? data?.tv_confirmed ?? data?.confirmed ?? confirmed,
    rejected: data?.rejected_count ?? data?.rejected ?? Math.max(rows.length - confirmed - failed, 0),
    failed: data?.technical_failed_count ?? data?.failed ?? data?.technical_failed ?? failed,
    timeframes: Array.isArray(data?.timeframes_checked) ? data.timeframes_checked.join(",") : data?.timeframe ?? rows[0]?.timeframe ?? SWING_MTF_TIMEFRAMES,
  };
}

function SwingTrading({
  swingRows,
  swingSummary,
  latestSwingTvRows,
  swingTvRowsLoaded,
  swingBatchResults,
  swingBatchProgress,
  swingBatchError,
  swingBatchStopRequested,
  swingBatchStopMessage,
  swingBatchRunning,
  candidatesStale,
  onSummary,
  onLoad,
  onBatchConfirm,
  onStopBatch,
  onLoadSaved,
  onOpenStock,
  loading,
  tvRuntimeStatus,
  tvRuntimeLastUpdatedAt,
  onRefreshTvStatus,
}) {
  const [selectedSwingRow, setSelectedSwingRow] = useState(null);
  const resultRows = sortByTradeQuality(flattenMtfRows(latestSwingTvRows));
  const sortedSwingRows = [...(Array.isArray(swingRows) ? swingRows : [])].sort((a, b) => Number(b?.score || 0) - Number(a?.score || 0));
  const swingCandidateCount = countValue(swingSummary?.swing_candidates_count ?? sortedSwingRows.length);
  const selectedRow = selectedSwingRow;
  const openSwingStock = (row) => {
    setSelectedSwingRow(row);
    onOpenStock?.(row);
  };
  return <div className="pageStack">
    <TvDiagnosticWarning status={tvRuntimeStatus} />
    <div className="warningText">TradingView confirmation only. No broker orders. No live trading.</div>
    {candidatesStale && <div className="warningText">Swing candidates may be stale. Run Score Market Data.</div>}
    <div className="tradingActionPanel">
      <div className="buttonRow tradingButtonRow">
        <ActionButton onClick={onSummary} disabled={loading}>Load Swing Summary</ActionButton>
        <ActionButton onClick={onLoad} disabled={loading}>Load Swing Candidates</ActionButton>
        <ActionButton onClick={onRefreshTvStatus} disabled={loading}>
          {loading === "refresh tv status" ? "Refreshing Status..." : "Refresh TradingView Status"}
        </ActionButton>
      </div>
      <div className="buttonRow tradingButtonRow tradingBatchActions">
        <ActionButton onClick={onBatchConfirm} disabled={loading || !isBatchReady(tvRuntimeStatus, tvRuntimeLastUpdatedAt) || deriveTradingViewBusy(tvRuntimeStatus)}>
          Run Swing TV Confirm in Batches
        </ActionButton>
        <ActionButton onClick={onStopBatch} disabled={!swingBatchRunning || swingBatchStopRequested}>Stop After Current Batch</ActionButton>
        <ActionButton onClick={onLoadSaved} disabled={loading}>Load Saved Swing TV Results</ActionButton>
      </div>
    </div>
    <BatchProgressNotice progress={swingBatchProgress} />
    {swingBatchStopRequested && !swingBatchStopMessage && <div className="warningText">Stop requested. Current batch will finish, then processing will stop.</div>}
    {swingBatchStopMessage && <div className="warningText">{swingBatchStopMessage}</div>}
    {swingBatchError && <div className="errorPanel">Batch {swingBatchError.batch_number} failed: {swingBatchError.message}</div>}
    <SummaryCards data={swingSummary} fields={["total_scored", "swing_candidates_count", "below_threshold_count", "invalid_count", "top_score"]} />
    {swingTvRowsLoaded ? (
      <div className="statsGrid compact" style={{ marginTop: "1rem", marginBottom: "1rem" }}>
        <StatCard label="TV Confirmed / Watch" value={resultRows.filter((r) => SWING_CONFIRMED_STATUSES.has(savedStatus(r)) || SWING_WAIT_WATCH_STATUSES.has(savedStatus(r))).length} tone="green" />
        <StatCard label="TV Rejected" value={resultRows.filter((r) => savedStatus(r) === "REJECTED").length} tone="red" />
        <StatCard label="TV Technical Failed" value={resultRows.filter((r) => savedStatus(r) === "TECHNICAL_FAILED").length} tone="red" />
      </div>
    ) : (
      <div className="warningText" style={{ marginTop: "1rem", marginBottom: "1rem" }}>
        Saved Swing TV status counts are currently unavailable. Click "Load Saved Swing TV Results" to retrieve them.
      </div>
    )}
    <Card title="Swing Candidates" eyebrow="watchlist">
      {sortedSwingRows.length ? <CandidateWatchlistGrid rows={sortedSwingRows} selectedRow={selectedRow} onSelect={openSwingStock} mode="swing" /> : <p className="muted">Click Load Swing Candidates to view swing watchlist.</p>}
      {selectedRow && <div className="compactDetails">
        <DetailGrid data={selectedRow} fields={["symbol", "tradingview_symbol", "score", "nse_score", "swing_status", "selected_for_tv", "current_price", "change_percent", "relative_volume", "thirty_day_change_percent", "source_used", "updated_at"]} />
        <details className="debug"><summary>Score Breakdown</summary><pre>{JSON.stringify(selectedRow.score_breakdown || selectedRow?.score_breakdown?.swing || {}, null, 2)}</pre></details>
        <details className="debug"><summary>Show Raw JSON</summary><pre>{JSON.stringify(selectedRow, null, 2)}</pre></details>
      </div>}
    </Card>
    {swingBatchResults.length > 0 && <>
      <SymbolFixWarnings rows={resultRows} />
      <WeeklyHistoryNotice rows={resultRows} />
    </>}
    <TvResultSummaryPanel title="Saved / Latest Swing TV Results" rows={resultRows} mode="swing" loaded={swingTvRowsLoaded} candidateCount={swingCandidateCount} onOpenStock={onOpenStock} />
  </div>;
}

function MomentumTrading({
  momentumRows,
  momentumSummary,
  latestMomentumTvRows,
  momentumTvRowsLoaded,
  momentumBatchResults,
  momentumBatchProgress,
  momentumBatchError,
  momentumBatchStopRequested,
  momentumBatchStopMessage,
  momentumBatchRunning,
  candidatesStale,
  onSummary,
  onLoad,
  onBatchConfirm,
  onStopBatch,
  onLoadSaved,
  onOpenStock,
  loading,
  tvRuntimeStatus,
  tvRuntimeLastUpdatedAt,
  onRefreshTvStatus,
}) {
  const [selectedMomentumRow, setSelectedMomentumRow] = useState(null);
  const sortedMomentumRows = [...(Array.isArray(momentumRows) ? momentumRows : [])].sort((a, b) => Number(b?.momentum_score || 0) - Number(a?.momentum_score || 0));
  const momentumCandidateCount = countValue(momentumSummary?.momentum_candidates_count ?? sortedMomentumRows.length);
  const selectedRow = selectedMomentumRow;
  const resultRows = sortByTradeQuality(flattenMomentumMtfRows(latestMomentumTvRows));
  const openMomentumStock = (row) => {
    setSelectedMomentumRow(row);
    onOpenStock?.(row);
  };
  return <div className="pageStack">
    <TvDiagnosticWarning status={tvRuntimeStatus} />
    <div className="warningText">Candidates are scanner outputs only. No broker orders. No live trading.</div>
    {candidatesStale && <div className="warningText">{MOMENTUM_SCORE_STALE_MESSAGE}</div>}
    <div className="tradingActionPanel">
      <div className="buttonRow tradingButtonRow">
        <ActionButton onClick={onSummary} disabled={loading}>Load Momentum Summary</ActionButton>
        <ActionButton onClick={onLoad} disabled={loading}>Load Momentum Candidates</ActionButton>
        <ActionButton onClick={onRefreshTvStatus} disabled={loading}>
          {loading === "refresh tv status" ? "Refreshing Status..." : "Refresh TradingView Status"}
        </ActionButton>
      </div>
      <div className="buttonRow tradingButtonRow tradingBatchActions">
        <ActionButton onClick={onBatchConfirm} disabled={loading || !isBatchReady(tvRuntimeStatus, tvRuntimeLastUpdatedAt) || deriveTradingViewBusy(tvRuntimeStatus)}>
          Run Momentum TV Confirm in Batches
        </ActionButton>
        <ActionButton onClick={onStopBatch} disabled={!momentumBatchRunning || momentumBatchStopRequested}>Stop After Current Batch</ActionButton>
        <ActionButton onClick={onLoadSaved} disabled={loading}>Load Saved Momentum TV Results</ActionButton>
      </div>
    </div>
    <BatchProgressNotice progress={momentumBatchProgress} />
    {momentumBatchStopRequested && !momentumBatchStopMessage && <div className="warningText">Stop requested. Current batch will finish, then processing will stop.</div>}
    {momentumBatchStopMessage && <div className="warningText">{momentumBatchStopMessage}</div>}
    {momentumBatchError && <div className="errorPanel">Batch {momentumBatchError.batch_number} failed: {momentumBatchError.message}</div>}
    <SummaryCards data={momentumSummary} fields={["total_scored", "momentum_candidates_count", "below_threshold_count", "overextended_count", "invalid_count", "top_momentum_score"]} />
    {momentumTvRowsLoaded ? (
      <div className="statsGrid compact" style={{ marginTop: "1rem", marginBottom: "1rem" }}>
        <StatCard label="TV Confirmed / Watch" value={resultRows.filter((r) => MOMENTUM_CONFIRMED_STATUSES.has(savedStatus(r)) || MOMENTUM_WAIT_WATCH_STATUSES.has(savedStatus(r))).length} tone="green" />
        <StatCard label="TV Rejected" value={resultRows.filter((r) => savedStatus(r) === "REJECTED").length} tone="red" />
        <StatCard label="TV Technical Failed" value={resultRows.filter((r) => savedStatus(r) === "TECHNICAL_FAILED").length} tone="red" />
      </div>
    ) : (
      <div className="warningText" style={{ marginTop: "1rem", marginBottom: "1rem" }}>
        Saved Momentum TV status counts are currently unavailable. Click "Load Saved Momentum TV Results" to retrieve them.
      </div>
    )}
    <Card title="Momentum Candidates" eyebrow="watchlist">
      {sortedMomentumRows.length ? <CandidateWatchlistGrid rows={sortedMomentumRows} selectedRow={selectedRow} onSelect={openMomentumStock} mode="momentum" /> : <p className="muted">Click Load Momentum Candidates to view momentum watchlist.</p>}
      {selectedRow && <div className="compactDetails">
        <DetailGrid data={{ ...selectedRow, overextended: selectedRow?.overextended === true || selectedRow?.momentum_status === "MOMENTUM_OVEREXTENDED" }} fields={["symbol", "tradingview_symbol", "momentum_score", "momentum_status", "momentum_candidate", "overextended", "current_price", "change_percent", "relative_volume", "thirty_day_change_percent", "source_used", "updated_at"]} />
        <details className="debug"><summary>Score Breakdown</summary><pre>{JSON.stringify(selectedRow.score_breakdown || selectedRow?.score_breakdown?.momentum || {}, null, 2)}</pre></details>
        <details className="debug"><summary>Show Raw JSON</summary><pre>{JSON.stringify(selectedRow, null, 2)}</pre></details>
      </div>}
    </Card>
    {momentumBatchResults.length > 0 && <>
      <SymbolFixWarnings rows={resultRows} />
    </>}
    <TvResultSummaryPanel title="Saved / Latest Momentum TV Results" rows={resultRows} mode="momentum" loaded={momentumTvRowsLoaded} candidateCount={momentumCandidateCount} onOpenStock={onOpenStock} />
  </div>;
}

function MarketLoadStats({ result, progress }) {
  if (!result && !progress) return null;
  return <div className="marketLoadStats">
    <StatCard label="Universe" value={result?.universe_count ?? progress?.universe_count ?? "--"} />
    <StatCard label="Processed" value={result?.processed ?? result?.planned_count ?? "--"} />
    <StatCard label="Complete" value={result?.complete_count ?? progress?.complete_count ?? "--"} />
    <StatCard label="Failed" value={result?.fetch_failed ?? "--"} tone="red" />
    <StatCard label="Upserted" value={result?.upserted_count ?? "--"} />
    <StatCard label="Modified" value={result?.modified_count ?? "--"} tone="yellow" />
    <StatCard label="Duration" value={result?.duration_seconds ? `${result.duration_seconds}s` : "--"} tone="yellow" />
    <StatCard label="NSE Quotes" value={result?.nse_batch?.quotes_count ?? "--"} />
    <StatCard label="YF Filled" value={result?.yfinance_batch?.completed_symbols ?? "--"} tone="yellow" />
    <StatCard label="Mongo Count" value={progress?.market_data_count ?? "--"} />
  </div>;
}

function ScoreRunStats({ result }) {
  if (!result) return null;
  return <div className="marketLoadStats">
    <StatCard label="Processed" value={result.processed ?? "--"} />
    <StatCard label="Scored Count" value={result.scored_count ?? "--"} />
    <StatCard label="Swing Candidates" value={result.swing_candidates_count ?? "--"} tone="yellow" />
    <StatCard label="Momentum Candidates" value={result.momentum_candidates_count ?? "--"} />
    <StatCard label="Both Candidates" value={result.both_candidates_count ?? "--"} tone="yellow" />
    <StatCard label="Invalid Count" value={result.invalid_count ?? "--"} tone="red" />
    <StatCard label="Duration" value={result.duration_seconds ? `${result.duration_seconds}s` : "--"} />
    <StatCard label="Scored At" value={result.frontend_scored_at ?? "--"} tone="yellow" />
  </div>;
}

function DetailGrid({ data, fields = [] }) {
  const visibleFields = fields.filter((field) => hasValue(data?.[field]));
  if (!visibleFields.length) return null;
  return <div className="detailGrid">{visibleFields.map((field) => (
    <div key={field}><span>{field}</span><strong>{displayValue(data?.[field])}</strong></div>
  ))}</div>;
}

const CONFIRMED_PAPER_STATUSES = new Set(["CONFIRMED_SIGNAL", "MOMENTUM_CONFIRMED"]);
const WAIT_PAPER_STATUSES = new Set(["WAIT_FOR_PULLBACK", "WAIT_FOR_RETEST", "WATCH_FOR_PULLBACK", "WATCH_FOR_BREAKOUT"]);
const FAILED_PAPER_STATUSES = new Set(["REJECTED", "TECHNICAL_FAILED"]);
const PROJECTED_MISSING_TEXT = "Projected numeric levels unavailable. Run TV Confirm again or candle/ATR data is missing.";
const NO_PLAN_AVAILABLE_TEXT = "No paper trade plan available. Run TV Confirm again for this stock.";

function hasAnyPlanField(row, fields) {
  return fields.some((field) => hasValue(row?.[field]));
}

function rrDisplay(...values) {
  const cleanValues = values.filter(hasValue).map((value) => fmt(value));
  return cleanValues.length ? cleanValues.join(" / ") : null;
}

function PlanMetricCard({ label, value, detail, tone = "green" }) {
  if (!hasValue(value)) return null;
  return <div className={`paperPlanMetric accent-${tone}`}>
    <span>{label}</span>
    <strong>{fmt(value)}</strong>
    {hasValue(detail) && <p>{displayValue(detail)}</p>}
  </div>;
}

function PaperTradePlan({ title, row }) {
  const hasRow = row && Object.keys(row).length > 0;
  if (!hasRow) {
    return <section className="paperPlanPanel">
      <div className="paperPlanTitle"><span>paper plan</span><h3>{title} Paper Trade Plan</h3></div>
      <div className="warningText">{NO_PLAN_AVAILABLE_TEXT}</div>
    </section>;
  }
  const status = savedStatus(row);
  const statusText = String(status || "").toUpperCase();
  const confirmedPlan = CONFIRMED_PAPER_STATUSES.has(statusText);
  const waitSetup = WAIT_PAPER_STATUSES.has(statusText) || String(row?.entry_readiness || "").toUpperCase().includes("WAIT");
  const failedPlan = FAILED_PAPER_STATUSES.has(statusText);
  const validPlan = row?.paper_plan_valid === true;
  const highRiskPlan = ["fake_breakout_risk", "retail_trap_risk"].some((field) => String(row?.[field] || "").toUpperCase().includes("HIGH"))
    || String(row?.trap_status || "").toUpperCase().includes("DANGER");
  const cautionPlan = validPlan && highRiskPlan;
  const cleanValidPlan = validPlan && !highRiskPlan;
  const planStatus = cautionPlan ? "CAUTION PLAN" : cleanValidPlan ? "Valid plan" : waitSetup ? "Wait setup" : failedPlan ? "No paper trade plan" : "Invalid plan";
  const badgeTone = statusText === "TECHNICAL_FAILED" ? "gray" : cautionPlan ? "yellow" : cleanValidPlan ? "green" : waitSetup ? "yellow" : "red";
  const hasConfirmedLevels = hasAnyPlanField(row, ["paper_entry_price", "paper_stop_loss", "paper_target_1", "paper_target_2", "paper_target_3"]);
  const hasWaitFields = hasAnyPlanField(row, ["entry_zone", "pullback_zone", "trigger_condition", "stop_loss_logic", "target_logic", "invalidation_condition", "next_action_for_paper_trade"]);
  const hasProjectedLevels = hasAnyPlanField(row, ["projected_entry_price", "projected_stop_loss", "projected_target_1", "projected_target_2", "projected_target_3"]);
  const hasQualityFields = hasAnyPlanField(row, ["trade_quality_grade", "quality_score", "quality_reason", "avoid_reason"]);
  const planReasonText = String(row?.paper_plan_reason || "");
  const projectedReasonText = String(row?.projected_plan_reason || "");
  const blockedByResistance = /WAIT_FOR_CLEAR_2R_SPACE|TARGET_BLOCKED_BY_RESISTANCE_BEFORE_2R|PROJECTED_2R_BLOCKED_BY_RESISTANCE|RR_BELOW_2/i.test(`${planReasonText} ${projectedReasonText}`);

  const missingConfirmedLevels = confirmedPlan && !hasConfirmedLevels;
  const missingWaitSetup = waitSetup && !hasWaitFields && !hasProjectedLevels;
  const missingGenericPlan = !confirmedPlan && !waitSetup && !failedPlan && !hasConfirmedLevels && !hasProjectedLevels && !hasWaitFields;
  const rrStatusDetail = hasValue(row?.paper_plan_reason)
    ? `${cautionPlan ? "caution" : validPlan ? "valid" : "invalid"} - ${displayValue(row.paper_plan_reason)}`
    : cautionPlan ? "caution" : validPlan ? "valid" : "invalid";

  return <section className="paperPlanPanel">
    <div className="paperPlanTitle"><span>paper plan</span><h3>{title} Paper Trade Plan</h3></div>
    <div className="paperPlanHeader">
      <Badge tone={badgeTone}>{planStatus}</Badge>
      {hasValue(status) && <Badge tone={statusTone(status)}>{displayValue(status)}</Badge>}
      {hasValue(row?.trade_quality_grade) && <Badge tone={qualityTone(row.trade_quality_grade)}>{qualityLabel(row.trade_quality_grade)}</Badge>}
    </div>
    {hasQualityFields && <div className="paperPlanCardGrid">
      <PlanMetricCard label="Trade Quality Grade" value={qualityLabel(row?.trade_quality_grade)} tone={qualityTone(row?.trade_quality_grade)} />
      <PlanMetricCard label="High Quality Trade Allowed" value={hasValue(row?.high_quality_trade_allowed) ? row?.high_quality_trade_allowed ? "yes" : "no" : null} tone={row?.high_quality_trade_allowed ? "green" : "yellow"} />
      <PlanMetricCard label="Quality Reason" value={row?.quality_reason} tone={qualityTone(row?.trade_quality_grade)} />
      {hasValue(row?.avoid_reason) && <PlanMetricCard label="Avoid Reason" value={row?.avoid_reason} tone="red" />}
    </div>}
    {(missingConfirmedLevels || missingWaitSetup || missingGenericPlan) && <div className="warningText">{NO_PLAN_AVAILABLE_TEXT}</div>}
    {cautionPlan && <div className="warningText">Entry/SL/Targets are mathematically valid, but fake breakout/trap risk is high. Wait for stronger confirmation before paper entry.</div>}
    {failedPlan && <div className="paperPlanState">
      <strong>No paper trade plan.</strong>
      <DetailGrid data={{
        paper_plan_reason: row?.paper_plan_reason,
        reason: row?.reason,
        rejection_reason: row?.rejection_reason,
        technical_reason: row?.error,
      }} fields={["paper_plan_reason", "reason", "rejection_reason", "technical_reason"]} />
    </div>}
    {!failedPlan && waitSetup && hasWaitFields && <div className="paperPlanState">
      <strong>No Immediate Entry - Wait Setup</strong>
      <p className="paperPlanSubtext">Wait. These are projected levels after trigger confirmation.</p>
      <h4>Projected Paper Levels - Valid Only After Trigger</h4>
      {blockedByResistance && <div className="paperPlanBlockedWarning">2R target is blocked by nearby resistance. No valid paper trade yet.</div>}
      {planReasonText.includes("WAIT_FOR_CLEAR_2R_SPACE") && <div className="warningText">Wait for clear 1:2 risk-reward space before paper entry.</div>}
      {!hasProjectedLevels && <div className="warningText">{PROJECTED_MISSING_TEXT}</div>}
      {hasProjectedLevels && <div className="paperPlanCardGrid">
        <PlanMetricCard label="Trigger Entry" value={row?.projected_entry_price} tone="yellow" />
        <PlanMetricCard label="Projected Stop-loss" value={row?.projected_stop_loss} tone="red" />
        <PlanMetricCard label="Projected T1" value={row?.projected_target_1} tone="green" />
        <PlanMetricCard label="Projected T2" value={row?.projected_target_2} tone="green" />
        <PlanMetricCard label="Projected T3" value={row?.projected_target_3} tone="green" />
        <PlanMetricCard label="Projected Risk / Share" value={row?.projected_risk_per_share} detail={row?.fallback_buffer_used ? "Fallback buffer used because ATR was missing." : null} tone="yellow" />
        <PlanMetricCard label="Projected RR" value={rrDisplay(row?.projected_rr_1, row?.projected_rr_2, row?.projected_rr_3)} tone={row?.projected_plan_valid ? "green" : "red"} />
        <PlanMetricCard label="2R Space Check" value={blockedByResistance ? "Blocked" : val(row?.projected_plan_reason || "Clear after trigger")} detail={blockedByResistance ? "No valid paper trade until resistance clears before 2R." : null} tone={blockedByResistance ? "red" : "yellow"} />
      </div>}
      <div className="paperPlanCardGrid">
        <PlanMetricCard label="Trigger Zone" value={row?.entry_zone} tone="yellow" />
        <PlanMetricCard label="Pullback Zone" value={row?.pullback_zone} tone="yellow" />
        <PlanMetricCard label="Trigger After Pullback" value={row?.trigger_condition} tone="yellow" />
        <PlanMetricCard label="Planned Stop-loss Logic" value={row?.stop_loss_logic} tone="yellow" />
        <PlanMetricCard label="Planned Target Logic" value={row?.target_logic} tone="yellow" />
        <PlanMetricCard label="Exit / Invalidation" value={row?.invalidation_condition} detail={row?.next_action_for_paper_trade} tone="red" />
        <PlanMetricCard label="Next Action" value={row?.next_action_for_paper_trade} tone="yellow" />
      </div>
    </div>}
    {!failedPlan && !waitSetup && hasConfirmedLevels && <div className="paperPlanCardGrid">
      <PlanMetricCard label="Entry" value={row?.paper_entry_price} detail={row?.entry_condition} tone="green" />
      <PlanMetricCard label="Stop-loss" value={row?.paper_stop_loss} detail={row?.stop_loss_logic} tone="red" />
      <PlanMetricCard label="Target 1" value={row?.paper_target_1} tone="green" />
      <PlanMetricCard label="Target 2" value={row?.paper_target_2} tone="green" />
      <PlanMetricCard label="Target 3" value={row?.paper_target_3} tone="green" />
      <PlanMetricCard label="Risk / Share" value={row?.paper_risk_per_share} tone={cautionPlan ? "yellow" : validPlan ? "green" : "red"} />
      <PlanMetricCard label="RR" value={rrDisplay(row?.paper_rr_1, row?.paper_rr_2, row?.paper_rr_3)} detail={rrStatusDetail} tone={cautionPlan ? "yellow" : validPlan ? "green" : "red"} />
      <PlanMetricCard label="Exit / Invalidation" value={row?.invalidation_condition} detail={row?.next_action_for_paper_trade} tone="red" />
      {row?.calculation_version === 2 && <>
        <PlanMetricCard label="Technical SL" value={row?.technical_stop_loss} detail={row?.stop_loss_basis} tone="yellow" />
        <PlanMetricCard label="SL Basis" value={row?.stop_loss_basis} tone="yellow" />
        <PlanMetricCard label="SL Overridden?" value={row?.stop_loss_overridden ? "Yes" : "No"} detail={row?.stop_loss_override_reason} tone={row?.stop_loss_overridden ? "yellow" : "green"} />
        <PlanMetricCard label="Raw Targets" value={`T1: ${row?.t1_target_raw} | T2: ${row?.t2_target_raw} | T3: ${row?.t3_target_raw}`} tone="yellow" />
        <PlanMetricCard label="T1 Details" value={`Final: ${row?.t1_target_final} (${row?.t1_final_rr}R)`} detail={`Conf: ${row?.t1_confidence} | Basis: ${row?.t1_structure_basis} | Flag: ${row?.t1_flag}`} tone="green" />
        <PlanMetricCard label="T2 Details" value={`Final: ${row?.t2_target_final} (${row?.t2_final_rr}R)`} detail={`Conf: ${row?.t2_confidence} | Basis: ${row?.t2_structure_basis} | Flag: ${row?.t2_flag}`} tone="green" />
        <PlanMetricCard label="T3 Details" value={`Final: ${row?.t3_target_final} (${row?.t3_final_rr}R)`} detail={`Conf: ${row?.t3_confidence} | Basis: ${row?.t3_structure_basis} | Flag: ${row?.t3_flag}`} tone="green" />
        <PlanMetricCard label="Risk Budget" value={row?.risk_budget} tone="green" />
        <PlanMetricCard label="Quantity Ceilings" value={`Risk: ${row?.quantity_by_risk} | Grade: ${row?.quantity_by_grade_margin} | Avail: ${row?.quantity_by_available_margin}`} tone="yellow" />
        <PlanMetricCard label="Min Allowed Qty" value={row?.minimum_allowed_quantity} detail={`Req Margin Qty: ${row?.quantity_for_minimum_margin}`} tone="yellow" />
        <PlanMetricCard label="Final Qty" value={row?.final_quantity} tone="green" />
        <PlanMetricCard label="Max Loss" value={row?.maximum_loss} tone="red" />
        <PlanMetricCard label="Partial Quantities" value={`T1: ${row?.t1_quantity} | T2: ${row?.t2_quantity} | T3: ${row?.t3_quantity}`} detail={`Reason: ${row?.allocation_reason}`} tone="green" />
        {row?.block_code && <PlanMetricCard label="Block Reason" value={row?.block_code} detail={row?.block_message} tone="red" />}
      </>}
    </div>}
  </section>;
}

function latestSavedForSearch(data, searched) {
  const rows = tvRows(data);
  return rows.find((row) => {
    const rowSymbol = String(row?.symbol || "").toUpperCase();
    const rowTv = String(row?.tradingview_symbol || "").toUpperCase();
    return rowSymbol === searched.symbol || rowTv === searched.tradingview_symbol;
  }) || null;
}

function TvSafetyDiagnostics({ title, rows }) {
  const safeRows = Array.isArray(rows) ? rows : [];
  if (!safeRows.length) return null;
  const symbolRows = safeRows.map((row) => ({
    symbol: row?.symbol,
    tradingview_symbol: row?.tradingview_symbol,
    symbol_match: row?.symbol_match,
    symbol_stable_check_passed: row?.symbol_stable_check_passed,
    stale_previous_symbol_warning: row?.stale_previous_symbol_warning,
    same_last_ohlcv_pairs: row?.candle_integrity_summary?.same_last_ohlcv_pairs ?? row?.same_last_ohlcv_pairs,
    possible_stale_pairs: row?.candle_integrity_summary?.possible_stale_pairs ?? row?.possible_stale_pairs,
    warnings: row?.candle_integrity_summary?.warnings,
  }));
  const timeframeRows = safeRows.flatMap((row) => Object.entries(row?.timeframe_debug || row?.timeframe_analysis || {}).map(([timeframe, debug]) => ({
    symbol: row?.symbol,
    timeframe,
    resolution_match: debug?.resolution_match,
    stable_check_passed: debug?.stable_check_passed,
    stale_or_merged_candle_warning: debug?.stale_or_merged_candle_warning,
    error_stage: debug?.error_stage,
    error_message: debug?.error_message,
  })));
  const symbolColumns = ["symbol", "tradingview_symbol", "symbol_match", "symbol_stable_check_passed", "stale_previous_symbol_warning", "same_last_ohlcv_pairs", "possible_stale_pairs", "warnings"];
  const timeframeColumns = ["symbol", "timeframe", "resolution_match", "stable_check_passed", "stale_or_merged_candle_warning", "error_stage", "error_message"];
  const hasSymbolRows = symbolRows.some((row) => hasFieldValue(row, symbolColumns));
  const hasTimeframeRows = timeframeRows.some((row) => hasFieldValue(row, timeframeColumns));
  if (!hasSymbolRows && !hasTimeframeRows) return null;
  return <Card title={title} eyebrow="TradingView safety">
    {hasSymbolRows && <MiniTable rows={symbolRows} columns={symbolColumns} />}
    {hasTimeframeRows && <MiniTable rows={timeframeRows} columns={timeframeColumns} />}
  </Card>;
}

function StockLearningSummary({ swingRow, momentumRow }) {
  const swingStatus = swingRow?.tv_status || swingRow?.swing_status || "NOT_CHECKED";
  const momentumStatus = momentumRow?.tv_status || momentumRow?.momentum_status || "NOT_CHECKED";
  const mainRisk = swingRow?.fake_breakout_risk || momentumRow?.fake_breakout_risk || momentumRow?.trap_status || swingRow?.retail_trap_risk || "-";
  const watchNext = String(swingStatus).includes("WAIT") || String(momentumStatus).includes("WAIT")
    ? "Wait for cleaner entry confirmation."
    : String(swingStatus).includes("CONFIRMED") || String(momentumStatus).includes("CONFIRMED")
      ? "Review risk and paper-only plan readiness."
      : "Check rejection/technical reason before taking any paper action.";
  return <Card title="Final Learning Summary" eyebrow="paper interpretation">
    <DetailGrid data={{
      swing_view: swingStatus,
      momentum_view: momentumStatus,
      main_risk: mainRisk,
      what_to_watch_next: watchNext,
    }} fields={["swing_view", "momentum_view", "main_risk", "what_to_watch_next"]} />
  </Card>;
}

function combinedRiskBlockers(row, label) {
  if (!hasValue(row)) return [];
  const blockers = [];
  const status = savedStatus(row);
  const grade = normalizeTradeQualityGrade(row?.trade_quality_grade);
  if (String(row?.trap_status || "").toUpperCase() === "DANGER") blockers.push(`${label} trap danger blocks combined setup.`);
  if (String(row?.fake_breakout_risk || "").toUpperCase() === "HIGH") blockers.push(`${label} fake breakout risk is HIGH.`);
  if (String(row?.retail_trap_risk || "").toUpperCase() === "HIGH") blockers.push(`${label} retail trap risk is HIGH.`);
  if (grade === "NO_TRADE") blockers.push(`${label} is NO_TRADE.`);
  if (status === "TECHNICAL_FAILED") blockers.push(`${label} TV result is TECHNICAL_FAILED.`);
  if (status === "REJECTED") blockers.push(`${label} TV result is REJECTED.`);
  return blockers;
}

function combinedFinalDecision(swingRow, momentumRow) {
  const swingHasRow = hasValue(swingRow);
  const momentumHasRow = hasValue(momentumRow);
  const swingGrade = normalizeTradeQualityGrade(swingRow?.trade_quality_grade);
  const momentumGrade = normalizeTradeQualityGrade(momentumRow?.trade_quality_grade);
  const swingGood = swingGrade === "A_PLUS" || swingGrade === "A";
  const momentumGood = momentumGrade === "A_PLUS" || momentumGrade === "A";
  const swingBlockers = combinedRiskBlockers(swingRow, "Swing");
  const momentumBlockers = combinedRiskBlockers(momentumRow, "Momentum");
  const allBlockers = [...swingBlockers, ...momentumBlockers];

  if (allBlockers.length) {
    const bothBlocked = swingBlockers.length > 0 && momentumBlockers.length > 0;
    return {
      combined_decision: bothBlocked ? "AVOID" : "WATCH_ONLY",
      reason: allBlockers[0],
      next_action: bothBlocked
        ? "Avoid this combined setup until both sides clear high-risk blockers."
        : "Watch only. Wait for the blocked side to clear before paper entry.",
    };
  }
  if (swingGood && momentumGood) {
    return {
      combined_decision: "BEST_COMBINED_SETUP",
      reason: "Swing and Momentum are both A/A+ with no HIGH/DANGER risk.",
      next_action: "Review paper plan levels; paper mode only.",
    };
  }
  if (swingGood && !momentumHasRow) {
    return {
      combined_decision: "SWING_ONLY_WATCH",
      reason: "Swing is A/A+ but Momentum TV result is missing.",
      next_action: "Use Swing as watch-only until Momentum TV Confirm is available.",
    };
  }
  if (momentumGood && !swingHasRow) {
    return {
      combined_decision: "MOMENTUM_ONLY_WATCH",
      reason: "Momentum is A/A+ but Swing TV result is missing.",
      next_action: "Use Momentum as watch-only until Swing TV Confirm is available.",
    };
  }
  return {
    combined_decision: "WATCH_ONLY",
    reason: "Best combined A/A+ setup is not present.",
    next_action: "Wait for cleaner combined confirmation before paper entry.",
  };
}

function MarketDataPage({
  search,
  tv,
  setTv,
  tvResult,
  stockMarketData,
  stockSwingPrecheck,
  stockMomentumPrecheck,
  stockSwingTvResult,
  stockMomentumTvResult,
  stockSwingTimeframes,
  setStockSwingTimeframes,
  stockMomentumTimeframes,
  setStockMomentumTimeframes,
  onLoadStockMarket,
  onSwingPrecheck,
  onMomentumPrecheck,
  onStockSwingTvConfirm,
  onStockMomentumTvConfirm,
  onTest,
  onDryRun750,
  onScanAll750,
  onScoreMarketData,
  marketLoadResult,
  marketProgress,
  scoreRunResult,
  scoreSummary,
  marketDataNeedsScore,
  loading,
  loadingText,
  lastResponse,
}) {
  const candles = normalizeCandles(tvResult?.candles);
  const lastCandle = candles[candles.length - 1];
  const extractionMessage = tvResult?.candle_extraction_message
    || tvResult?.candle_extraction_method
    || tvResult?.diagnostics?.candle_extraction_method
    || tvResult?.diagnostics?.candle_extraction_error
    || "Waiting for TradingView candle extraction";
  return <div className="pageStack">
    <div className="warningText">Paper mode only. No broker orders. No live trading.</div>
    <Card title="Market Data Workflow" eyebrow="market_data to scored_candidates">
      <div className="marketLoadPanel">
        <div>
          <strong>Market Data Workflow</strong>
          <p>Step 1: Scan All 750 Stocks updates market_data. Step 2: Score Market Data updates scored_candidates. Step 3: Load Swing/Momentum Candidates from scored_candidates.</p>
          <p>Candidates are scanner outputs only. No broker orders. No live trading.</p>
          {loadingText?.includes("750") && <p className="scanStatusText">{loadingText === "scan all 750 stocks" ? "Scanning all 750 stocks..." : `Loading ${loadingText}...`}</p>}
          {loadingText === "score market data" && <p className="scanStatusText">Scoring market_data rows...</p>}
        </div>
        <div className="buttonColumn">
          <ActionButton onClick={onScanAll750} disabled={loading}>Scan All 750 Stocks</ActionButton>
          <ActionButton onClick={onScoreMarketData} disabled={loading}>Score Market Data</ActionButton>
        </div>
      </div>
      {marketDataNeedsScore && <div className="warningText">{SCAN_SCORE_WARNING}</div>}
    </Card>
    {(marketLoadResult || marketProgress) && <Card title="Market Data Load Result" eyebrow="market_data">
      <MarketLoadStats result={marketLoadResult} progress={marketProgress} />
      {marketProgress && <p className="chartMeta">Latest market_data update: {val(marketProgress.market_data_latest_updated_at ?? marketProgress.latest_updated_at)}</p>}
    </Card>}
    {scoreRunResult && <Card title="Score Market Data Result" eyebrow="scored_candidates">
      <ScoreRunStats result={scoreRunResult} />
      {scoreSummary && <p className="chartMeta">Latest scored_candidates update: {val(scoreSummary.scored_candidates_latest_updated_at ?? scoreSummary.latest_updated_at)}</p>}
    </Card>}
    <details className="debug developerDiagnostics">
      <summary>Developer Diagnostics</summary>
      <Card title="Dry Run 750 Scan" eyebrow="developer tool"><ActionButton onClick={onDryRun750} disabled={loading}>Dry Run 750 Scan</ActionButton></Card>
      <Card title="TradingView Desktop Symbol Control" eyebrow="hidden debug tool"><div className="formGrid"><label>Symbol<input value={tv.symbol} onChange={(e) => setTv({ ...tv, symbol: e.target.value })} /></label><label>Timeframe<input value={tv.timeframe} onChange={(e) => setTv({ ...tv, timeframe: e.target.value })} /></label><ActionButton onClick={onTest} disabled={loading}>Test TV Candles</ActionButton></div></Card>
      <div className="twoGrid"><Card title={tv.symbol} eyebrow="stock header"><div className="detailPanel"><strong>{tv.symbol}</strong><p>TradingView CDP analysis. Paper mode only.</p><Badge tone="yellow">No live orders</Badge></div></Card><Card title="Candle Count" eyebrow="OHLCV"><div className="candleCount">{val(tvResult?.candles_count ?? candles.length)}</div><Badge tone={tvResult?.symbol_loaded ? "green" : "yellow"}>{tvResult?.symbol_loaded ? "Loaded" : "Waiting"}</Badge><p className="chartMeta">{extractionMessage}</p></Card></div>
      {tvResult && !candles.length && <div className="errorPanel stockError">No candles available to render. {val(tvResult?.error)}</div>}
      <Card title="Candlestick Chart" eyebrow="last 60 candles"><CandleChart candles={candles} /></Card>
      <Card title="Last Candle OHLCV" eyebrow="latest bar"><LastCandleCard candle={lastCandle} /></Card>
      <Card title="TradingView Diagnostics"><pre className="terminalText">symbol_loaded: {val(tvResult?.symbol_loaded)}{"\n"}candles_count: {val(tvResult?.candles_count ?? candles.length)}{"\n"}connected: {val(tvResult?.connected)}{"\n"}method: {extractionMessage}{"\n"}error: {val(tvResult?.error)}</pre></Card>
      {lastResponse && <details className="debug"><summary>Last raw JSON</summary><pre>{JSON.stringify(lastResponse, null, 2)}</pre></details>}
    </details>
  </div>;
}

function StockDetailPage({
  search,
  stockMarketData,
  stockSwingPrecheck,
  stockMomentumPrecheck,
  stockSwingTvResult,
  stockMomentumTvResult,
  stockSavedSwingResult,
  stockSavedMomentumResult,
  latestSwingTvRows,
  latestMomentumTvRows,
  stockSwingTimeframes,
  setStockSwingTimeframes,
  stockMomentumTimeframes,
  setStockMomentumTimeframes,
  onLoadStockMarket,
  onSwingPrecheck,
  onMomentumPrecheck,
  onStockSwingTvConfirm,
  onStockMomentumTvConfirm,
  loading,
}) {
  const searched = normalizeSearchSymbol(search);
  const marketRow = stockMarketData?.row || {};
  const swingPrecheckRow = stockSwingPrecheck?.row || {};
  const momentumPrecheckRow = stockMomentumPrecheck?.row || {};
  const swingTvRows = flattenMtfRows(tvRows(stockSwingTvResult));
  const momentumTvRows = flattenMomentumMtfRows(tvRows(stockMomentumTvResult));
  const savedSwing = latestSavedForSearch(stockSavedSwingResult, searched) || latestSavedForSearch({ rows: latestSwingTvRows }, searched);
  const savedMomentum = latestSavedForSearch(stockSavedMomentumResult, searched) || latestSavedForSearch({ rows: latestMomentumTvRows }, searched);
  const savedSwingRows = savedSwing ? flattenMtfRows([savedSwing]) : [];
  const savedMomentumRows = savedMomentum ? flattenMomentumMtfRows([savedMomentum]) : [];
  const swingDetailRows = swingTvRows.length ? swingTvRows : savedSwingRows;
  const momentumDetailRows = momentumTvRows.length ? momentumTvRows : savedMomentumRows;
  const swingTvRow = swingDetailRows[0] || {};
  const momentumTvRow = momentumDetailRows[0] || {};
  const stale = stockSwingPrecheck?.is_score_stale || stockMomentumPrecheck?.is_score_stale;
  const scoreRow = {
    symbol: searched.symbol,
    tradingview_symbol: searched.tradingview_symbol,
    score: swingPrecheckRow?.score,
    nse_score: swingPrecheckRow?.nse_score,
    selected_for_tv: swingPrecheckRow?.selected_for_tv,
    score_reason: swingPrecheckRow?.reason || momentumPrecheckRow?.reason,
    swing_candidate: swingPrecheckRow?.swing_candidate,
    swing_status: swingPrecheckRow?.swing_status,
    momentum_candidate: momentumPrecheckRow?.momentum_candidate,
    momentum_status: momentumPrecheckRow?.momentum_status,
    momentum_score: momentumPrecheckRow?.momentum_score,
    overextended: momentumPrecheckRow?.overextended,
    score_breakdown: swingPrecheckRow?.score_breakdown || momentumPrecheckRow?.score_breakdown,
  };
  const swingView = swingTvRow?.tv_status || swingPrecheckRow?.swing_status || "NOT_CHECKED";
  const momentumView = momentumTvRow?.tv_status || momentumPrecheckRow?.momentum_status || "NOT_CHECKED";
  const combinedDecision = combinedFinalDecision(swingTvRow, momentumTvRow);
  const qualityCandidates = [swingTvRow?.trade_quality_grade, momentumTvRow?.trade_quality_grade].filter(Boolean);
  const qualityOrder = ["A_PLUS", "A", "B", "C", "NO_TRADE"];
  const tradeQualityGrade = qualityOrder.find((grade) => qualityCandidates.includes(grade)) || qualityCandidates[0];
  const mainRisk = swingTvRow?.avoid_reason
    || momentumTvRow?.avoid_reason
    || swingTvRow?.rejection_reason
    || momentumTvRow?.rejection_reason
    || swingTvRow?.paper_plan_reason
    || momentumTvRow?.paper_plan_reason
    || swingTvRow?.retail_trap_risk
    || swingTvRow?.fake_breakout_risk
    || momentumTvRow?.trap_status
    || momentumTvRow?.fake_breakout_risk
    || "-";
  const nextAction = swingTvRow?.next_action_for_paper_trade
    || momentumTvRow?.next_action_for_paper_trade
    || (String(swingView).includes("WAIT") || String(momentumView).includes("WAIT")
      ? "Wait for cleaner entry confirmation."
      : String(swingView).includes("CONFIRMED") || String(momentumView).includes("CONFIRMED")
        ? "Review risk and paper-only plan readiness."
        : "Check rejection/technical reason before taking any paper action.");
  const latestUpdatedAt = marketRow?.updated_at || swingPrecheckRow?.updated_at || momentumPrecheckRow?.updated_at || savedSwing?.updated_at || savedMomentum?.updated_at;
  const marketFields = ["current_price", "change_percent", "traded_volume", "relative_volume", "day_high", "day_low"];
  const scoreFields = ["score", "nse_score", "selected_for_tv", "swing_status", "momentum_status", "swing_candidate", "momentum_candidate"];
  const swingFields = ["tv_status", "confidence_score", "reason", "weekly_bias", "daily_setup", "four_hour_confirmation", "one_hour_entry", "risk_reward_1", "fake_breakout_risk", "retail_trap_risk", "trade_quality_grade"];
  const momentumFields = ["tv_status", "confidence_score", "reason", "daily_momentum", "four_hour_confirmation", "one_hour_entry", "entry_quality", "trap_status", "trap_reason", "trade_quality_grade"];
  const fullMarketFields = ["current_price", "previous_close", "open_price", "day_high", "day_low", "change_percent", "traded_volume", "traded_value", "relative_volume", "thirty_day_change_percent", "source_used", "primary_source", "field_sources", "nse_ok", "yfinance_ok", "nse_source_index", "nse_source_indexes", "is_complete", "missing_fields", "updated_at"];
  const swingPrecheckDebugFields = ["swing_score", "selected_for_tv", "swing_status", "reason", "eligible_for_tv_confirm", "score_breakdown"];
  const swingTvDebugFields = ["tv_status", "tv_confirmed", "swing_confirmed_at", "confirmed_at", "source_candle_at", "calculation_timestamp", "confidence_score", "reason", "rejection_reason", "weekly_bias", "daily_setup", "four_hour_confirmation", "one_hour_entry", "candles_1W", "candles_1D", "candles_4H", "candles_1H", "paper_plan_valid", "paper_plan_reason", "entry_readiness", "next_action_for_paper_trade", "fake_breakout_risk", "retail_trap_risk", "trade_quality_grade", "swing_explanation", "entry_comment", "error"];
  const momentumPrecheckDebugFields = ["momentum_score", "momentum_candidate", "momentum_status", "overextended", "eligible_for_tv_confirm", "score_breakdown"];
  const momentumTvDebugFields = ["tv_status", "tv_confirmed", "momentum_confirmed_at", "confirmed_at", "source_candle_at", "calculation_timestamp", "confidence_score", "reason", "rejection_reason", "daily_momentum", "four_hour_confirmation", "one_hour_entry", "candles_1D", "candles_4H", "candles_1H", "paper_plan_valid", "paper_plan_reason", "entry_readiness", "next_action_for_paper_trade", "fake_breakout_risk", "overextended_risk", "volume_confirmation", "entry_quality", "trap_status", "trap_reason", "momentum_trap_score", "momentum_trap_summary", "trade_quality_grade", "momentum_explanation", "entry_comment", "error"];
  const savedConfirmationFields = ["symbol", "tradingview_symbol", "tv_status", "trade_quality_grade", "reason", "confirmed_at", "swing_confirmed_at", "momentum_confirmed_at", "updated_at", "created_at", "source_candle_at", "calculation_timestamp"];
  const swingPrecheckDebugRow = { ...swingPrecheckRow, eligible_for_tv_confirm: swingPrecheckRow?.selected_for_tv === true ? "yes" : "no" };
  const momentumPrecheckDebugRow = { ...momentumPrecheckRow, eligible_for_tv_confirm: momentumPrecheckRow?.momentum_candidate === true ? "yes" : "no" };
  const hasMarketSummary = hasFieldValue(marketRow, marketFields);
  const hasScoreSummary = hasFieldValue(scoreRow, scoreFields);
  const hasSwingSummary = hasFieldValue(swingTvRow, swingFields);
  const hasMomentumSummary = hasFieldValue(momentumTvRow, momentumFields);
  const hasMarketDiagnostics = stockMarketData?.found && hasFieldValue(marketRow, fullMarketFields);
  const hasScoreDiagnostics = Boolean(stockSwingPrecheck?.found || stockMomentumPrecheck?.found);
  const hasSwingPrecheckDiagnostics = stockSwingPrecheck?.found && hasFieldValue(swingPrecheckDebugRow, swingPrecheckDebugFields);
  const hasSwingTvDiagnostics = swingDetailRows.some((row) => hasFieldValue(row, swingTvDebugFields));
  const hasMomentumPrecheckDiagnostics = stockMomentumPrecheck?.found && hasFieldValue(momentumPrecheckDebugRow, momentumPrecheckDebugFields);
  const hasMomentumTvDiagnostics = momentumDetailRows.some((row) => hasFieldValue(row, momentumTvDebugFields));
  const hasSavedDiagnostics = Boolean(savedSwing || savedMomentum);
  const rawDiagnosticsData = { market_data: stockMarketData, swing_precheck: stockSwingPrecheck, momentum_precheck: stockMomentumPrecheck, swing_tv: stockSwingTvResult, momentum_tv: stockMomentumTvResult, saved_swing: stockSavedSwingResult, saved_momentum: stockSavedMomentumResult };
  const hasRawDiagnostics = Object.values(rawDiagnosticsData).some(hasValue);
  const hasDiagnostics = hasMarketDiagnostics || hasScoreDiagnostics || hasSwingPrecheckDiagnostics || hasSwingTvDiagnostics || hasMomentumPrecheckDiagnostics || hasMomentumTvDiagnostics || hasSavedDiagnostics || hasRawDiagnostics;

  if (!searched.symbol) {
    return <div className="pageStack">
      <div className="warningText">Paper mode only. No broker orders. No live trading.</div>
      <Card title="Stock Detail" eyebrow="single stock"><p className="muted">Search a stock using the global search box to view complete stock details.</p></Card>
    </div>;
  }

  return <div className="pageStack">
    <div className="warningText">Paper mode only. No broker orders. No live trading.</div>
    <Card title="Stock Header" eyebrow="single stock">
      {stale && <div className="warningText">Scores may be stale. Run Score Market Data before trusting candidates.</div>}
      <div className="stockHero">
        <div><span>searched stock</span><strong>{marketRow?.tradingview_symbol || searched.tradingview_symbol}</strong>{hasValue(latestUpdatedAt) && <p>{displayValue(latestUpdatedAt)}</p>}</div>
        <Badge tone="yellow">PAPER MODE</Badge>
      </div>
      <DetailGrid data={{
        symbol: marketRow?.symbol || searched.symbol,
        tradingview_symbol: marketRow?.tradingview_symbol || searched.tradingview_symbol,
        exchange: marketRow?.exchange || searched.exchange,
        updated_at: latestUpdatedAt,
      }} fields={["symbol", "tradingview_symbol", "exchange", "updated_at"]} />
    </Card>

    <Card title="Combined Final Decision" eyebrow="paper decision">
      <DetailGrid data={{
        swing_view: swingView,
        momentum_view: momentumView,
        combined_decision: combinedDecision.combined_decision,
        reason: combinedDecision.reason,
        next_action: combinedDecision.next_action,
      }} fields={["swing_view", "momentum_view", "combined_decision", "reason", "next_action"]} />
    </Card>

    <Card title="Paper Trade Plan" eyebrow="entry, risk, targets">
      <PaperTradePlan title="Swing" row={swingTvRow} />
      <PaperTradePlan title="Momentum" row={momentumTvRow} />
    </Card>

    <Card title="Market Data" eyebrow="market_data">
      <div className="buttonRow compactButtons"><ActionButton onClick={onLoadStockMarket} disabled={loading}>Refresh Market Data</ActionButton></div>
      {hasMarketSummary ? <DetailGrid data={marketRow} fields={marketFields} /> : <p className="muted">Market data not loaded yet. Click Refresh Market Data.</p>}
    </Card>

    <Card title="Score / Precheck" eyebrow="scored_candidates">
      {hasScoreSummary ? <DetailGrid data={scoreRow} fields={scoreFields} /> : <p className="muted">Score/precheck data not loaded yet. Run Score Market Data first.</p>}
    </Card>

    <Card title="Swing TV Details" eyebrow="compact summary">
      <div className="buttonRow compactButtons">
        <ActionButton onClick={onSwingPrecheck} disabled={loading}>Swing Precheck</ActionButton>
        <ActionButton onClick={onStockSwingTvConfirm} disabled={loading}>Swing TV Confirm</ActionButton>
      </div>
      <div className="tvControlGrid stockTimeframeGrid"><label>Swing TV Timeframes<input value={stockSwingTimeframes} onChange={(event) => setStockSwingTimeframes(event.target.value)} /></label></div>
      <WeeklyHistoryNotice rows={swingDetailRows} />
      {hasSwingSummary ? <DetailGrid data={swingTvRow} fields={swingFields} /> : <p className="muted">No Swing TV result found. Run Swing TV Confirm for this stock.</p>}
    </Card>

    <Card title="Momentum TV Details" eyebrow="compact summary">
      <div className="buttonRow compactButtons">
        <ActionButton onClick={onMomentumPrecheck} disabled={loading}>Momentum Precheck</ActionButton>
        <ActionButton onClick={onStockMomentumTvConfirm} disabled={loading}>Momentum TV Confirm</ActionButton>
      </div>
      <div className="tvControlGrid stockTimeframeGrid"><label>Momentum TV Timeframes<input value={stockMomentumTimeframes} onChange={(event) => setStockMomentumTimeframes(event.target.value)} /></label></div>
      {hasMomentumSummary ? <DetailGrid data={momentumTvRow} fields={momentumFields} /> : <p className="muted">No Momentum TV result found. Run Momentum TV Confirm for this stock.</p>}
    </Card>

    {hasDiagnostics && <details className="debug developerDiagnostics">
      <summary>Developer Diagnostics</summary>
      {hasMarketDiagnostics && <Card title="Full Market Data" eyebrow="diagnostics">
        <MiniTable rows={[marketRow]} columns={fullMarketFields} />
      </Card>}
      {hasScoreDiagnostics && <Card title="Score Breakdown" eyebrow="diagnostics">
        <details className="debug"><summary>Score / Precheck Raw JSON</summary><pre>{JSON.stringify({ swing_precheck: stockSwingPrecheck, momentum_precheck: stockMomentumPrecheck }, null, 2)}</pre></details>
      </Card>}
      {(hasSwingPrecheckDiagnostics || hasSwingTvDiagnostics) && <Card title="Swing Diagnostics" eyebrow="diagnostics">
        {hasSwingPrecheckDiagnostics && <MiniTable rows={[swingPrecheckDebugRow]} columns={swingPrecheckDebugFields} />}
        {hasSwingTvDiagnostics && <MiniTable rows={swingDetailRows} columns={swingTvDebugFields} />}
        <TechnicalErrorDetails rows={swingDetailRows} />
      </Card>}
      {(hasMomentumPrecheckDiagnostics || hasMomentumTvDiagnostics) && <Card title="Momentum Diagnostics" eyebrow="diagnostics">
        {hasMomentumPrecheckDiagnostics && <MiniTable rows={[momentumPrecheckDebugRow]} columns={momentumPrecheckDebugFields} />}
        {hasMomentumTvDiagnostics && <MiniTable rows={momentumDetailRows} columns={momentumTvDebugFields} />}
        <TechnicalErrorDetails rows={momentumDetailRows} />
      </Card>}
      <TvSafetyDiagnostics title="Swing TV Safety" rows={swingDetailRows} />
      <TvSafetyDiagnostics title="Momentum TV Safety" rows={momentumDetailRows} />
      {hasSavedDiagnostics && <Card title="Latest Saved Confirmations" eyebrow="diagnostics">
        {savedSwing && <MiniTable rows={[savedSwing]} columns={savedConfirmationFields} />}
        {savedMomentum && <MiniTable rows={[savedMomentum]} columns={savedConfirmationFields} />}
      </Card>}
      {hasRawDiagnostics && <details className="debug"><summary>Full API Response / Raw JSON</summary><pre>{JSON.stringify(rawDiagnosticsData, null, 2)}</pre></details>}
    </details>}
  </div>;
}

const PAPER_TRADES_REFRESH_MS = 60000;
const PAPER_TABLE_COLUMNS = [
  { key: "symbol", label: "Symbol" },
  { key: "strategy", label: "Strategy", type: "strategy" },
  { key: "status", label: "Status", type: "status" },
  { key: "shares", label: "Shares", type: "shares" },
  { key: "entry", label: "Entry" },
  { key: "current_price", label: "Price", type: "price" },
  { key: "distance_to_entry", label: "Distance (₹)" },
  { key: "distance_to_entry_percent", label: "Distance (%)" },
  { key: "stop_loss", label: "Stop Loss" },
  { key: "target_1", label: "T1" },
  { key: "target_2", label: "T2" },
  { key: "target_3", label: "T3" },
  { key: "pnl", label: "P&L", type: "pnl" },
  { key: "reserved_margin", label: "Reserved Margin", type: "margin" },
  { key: "setup_time", label: "Setup Time" },
  { key: "setup_valid_until", label: "Setup Valid Until" },
  { key: "actions", label: "Actions", type: "actions" },
];
const PAPER_SORT_LABELS = {
  "setup_time|desc": "Newest setup first",
  "setup_time|asc": "Oldest setup first",
  "pnl|desc": "P&L high to low",
  "pnl|asc": "P&L low to high",
  "t1|asc": "T1 low to high",
  "t1|desc": "T1 high to low",
  "symbol|asc": "Symbol A-Z",
  "strategy|asc": "Strategy",
  "status|asc": "Status",
};
const PAPER_TRADE_FILTERS = [
  { key: "waiting", label: "Waiting for Entry", groups: new Set(["waiting"]) },
  { key: "active", label: "Active Trades", groups: new Set(["active"]) },
  { key: "completed", label: "Completed / Stopped", groups: new Set(["completed", "stopped", "ambiguous", "expired"]) },
  { key: "all", label: "All Trades", groups: null },
];
const PAPER_WAITING_STATUSES = new Set(["PLANNED", "NOT_TRIGGERED", "WAITING", "WAITING_FOR_ENTRY", "ENTRY_TRIGGERED", "WAITING_FOR_CAPITAL"]);
const PAPER_PARTIAL_STATUSES = new Set(["T1_PARTIAL", "T2_PARTIAL"]);
const PAPER_ACTIVE_STATUSES = new Set(["ACTIVE"]);
const PAPER_COMPLETED_STATUSES = new Set(["T3_HIT", "TARGET_3_HIT", "COMPLETED", "TARGET_HIT", "TARGET_1_HIT", "TARGET_1_HIT_FINAL", "TARGET_2_HIT", "T1_HIT", "T2_HIT", "WON_T1", "WON_T2", "WON_T3"]);
const PAPER_STOPPED_STATUSES = new Set(["SL_HIT", "STOPPED", "STOP_HIT", "STOPPED_AFTER_T1", "LOST_SL"]);
const PAPER_AMBIGUOUS_STATUSES = new Set(["AMBIGUOUS"]);
const PAPER_EXPIRED_STATUSES = new Set(["EXPIRED", "NOT_TRIGGERED", "ENTRY_MISSED_GAP_UP"]);

function paperTradeIdentity(trade, fallback = 0) {
  return trade?.paper_trade_id || trade?.trade_id || trade?.setup_id || trade?._id || trade?.id || `${trade?.symbol || "trade"}-${fallback}`;
}
function dedupePaperTrades(rows) {
  const seen = new Set();
  return (Array.isArray(rows) ? rows : []).filter((trade, index) => {
    const key = paperTradeIdentity(trade, index);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
function paperStrategyText(trade) {
  const raw = trade?.strategy || trade?.source_signal_type || trade?.strategy_type || trade?.source || "";
  const text = String(raw).toUpperCase();
  if (text.includes("MOMENTUM")) return "Momentum";
  if (text.includes("SWING")) return "Swing";
  return raw ? String(raw).replace(/_/g, " ") : "Other";
}
function paperStrategyTone(trade) {
  const text = paperStrategyText(trade).toUpperCase();
  if (text.includes("MOMENTUM")) return "yellow";
  if (text.includes("SWING")) return "green";
  return "gray";
}
function normalizePaperStatus(value) {
  return String(value || "").trim().toUpperCase().replace(/[\s-]+/g, "_");
}
function paperStatusSet(trade) {
  return new Set([trade?.status, trade?.outcome_status, trade?.ui_status].map(normalizePaperStatus).filter(Boolean));
}
function hasAnyPaperStatus(statuses, candidates) {
  for (const status of candidates) {
    if (statuses.has(status)) return true;
  }
  return false;
}
function paperDisplayStatus(trade) {
  if (trade?.outcome_label) return trade.outcome_label;
  if (trade?.status === "INVALIDATED_STALE" || trade?.invalidated_reason) return "Invalidated";
  const statuses = paperStatusSet(trade);
  if (hasAnyPaperStatus(statuses, PAPER_EXPIRED_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_ACTIVE_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_PARTIAL_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_COMPLETED_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_STOPPED_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_AMBIGUOUS_STATUSES)) return "Expired / Not Triggered";
  if (hasAnyPaperStatus(statuses, PAPER_WAITING_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_ACTIVE_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_PARTIAL_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_COMPLETED_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_STOPPED_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_AMBIGUOUS_STATUSES)) return "Waiting for Entry";
  if (hasAnyPaperStatus(statuses, PAPER_PARTIAL_STATUSES)) return "Partial";
  if (hasAnyPaperStatus(statuses, PAPER_COMPLETED_STATUSES)) return "Completed";
  if (hasAnyPaperStatus(statuses, PAPER_STOPPED_STATUSES)) {
    if (trade?.partial_exit_1 || trade?.t1_hit) return "T1 Hit → Stop Loss";
    if (trade?.exit_reason === "STOP_LOSS_HIT_BEFORE_ENTRY") return "Stopped Before Entry";
    return "Stopped";
  }
  if (hasAnyPaperStatus(statuses, PAPER_AMBIGUOUS_STATUSES)) return "Ambiguous";
  if (hasAnyPaperStatus(statuses, PAPER_ACTIVE_STATUSES)) return "Active";
  return trade?.ui_status || trade?.status || trade?.outcome_status || "-";
}
function paperStatusToneFromLabel(status) {
  const label = String(status || "").toLowerCase();
  if (label.includes("invalidated")) return "gray";
  if (label.includes("stopped") || label.includes("sl")) return "red";
  if (label.includes("expired") || label.includes("not triggered")) return "gray";
  if (label.includes("waiting") || label.includes("partial") || label.includes("ambiguous")) return "yellow";
  if (label.includes("active") || label.includes("completed")) return "green";
  return "gray";
}
function paperTradeMatchesFilters(trade, searchText, strategyFilter) {
  const strategy = paperStrategyText(trade);
  const searchable = [
    trade?.symbol,
    trade?.tradingview_symbol,
    trade?.source_signal_type,
    trade?.strategy_type,
    trade?.status,
    trade?.ui_status,
    trade?.outcome_status,
    trade?.setup_id,
    paperDisplayStatus(trade),
  ].join(" ").toLowerCase();
  const cleanSearch = searchText.trim().toLowerCase();
  const searchMatch = !cleanSearch || searchable.includes(cleanSearch);
  const strategyMatch = strategyFilter === "ALL" || strategy.toUpperCase().includes(strategyFilter);
  return searchMatch && strategyMatch;
}
function paperCellValue(row, column) {
  if (column.key === "strategy") return paperStrategyText(row);
  if (column.key === "status") return paperDisplayStatus(row);
  if (column.key === "entry") return row?.entry_price ?? row?.entry;
  if (column.key === "current_price") return row?.current_price ?? row?.latest_close;
  if (column.key === "stop_loss") return row?.current_stop_loss ?? row?.current_sl ?? row?.stop_loss ?? row?.sl;
  if (column.key === "target_1") return row?.target_1 ?? row?.t1;
  if (column.key === "target_2") return row?.target_2 ?? row?.t2;
  if (column.key === "target_3") return row?.target_3 ?? row?.t3;
  if (column.key === "pnl") {
    if (row?.status === "INVALIDATED_STALE" || row?.invalidated_reason) return 0;
    const statuses = paperStatusSet(row);
    if (hasAnyPaperStatus(statuses, PAPER_EXPIRED_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_COMPLETED_STATUSES) && !hasAnyPaperStatus(statuses, PAPER_STOPPED_STATUSES)) return row?.pnl_display ?? row?.paper_pnl ?? row?.pnl ?? 0;
    return row?.pnl_display ?? row?.paper_pnl ?? row?.pnl;
  }
  if (column.key === "setup_time") return row?.setup_time ?? row?.created_at ?? row?.source_confirmation_created_at;
  if (column.key === "setup_valid_until") return row?.setup_valid_until ?? row?.valid_until ?? row?.expires_at ?? row?.expiry_timestamp ?? "End of Day";
  if (column.key === "distance_to_entry") {
    const info = getActiveTradeDistanceInfo(row);
    if (info.distanceRupees !== null && info.distanceRupees !== undefined) {
      return Number(info.distanceRupees).toFixed(2);
    }
    const val = row?.distance_to_entry ?? (row?.entry_price && row?.current_price ? Math.abs((row?.entry_price ?? row?.entry) - (row?.current_price ?? row?.latest_close)) : null);
    return val !== null && val !== undefined ? Number(val).toFixed(2) : "—";
  }
  if (column.key === "distance_to_entry_percent") {
    const info = getActiveTradeDistanceInfo(row);
    if (info.distancePercent !== null && info.distancePercent !== undefined) {
      return `${Number(info.distancePercent).toFixed(2)}%`;
    }
    const val = row?.distance_to_entry_percent ?? (row?.entry_price && row?.current_price && row.entry_price > 0 ? (((row.entry_price - row.current_price) / row.entry_price) * 100) : null);
    return val !== null && val !== undefined ? `${Number(val).toFixed(2)}%` : "—";
  }
  if (column.key === "shares") return row;
  if (column.key === "reserved_margin") return row?.reserved_margin;
  return row?.[column.key];
}
function numericSortValue(value) {
  const parsed = Number(String(value ?? "").replace(/,/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}
function getActiveTradeDistanceInfo(row) {
  const normStatus = normalizePaperStatus(row?.status);
  const isWaiting = PAPER_WAITING_STATUSES.has(normStatus) || row?.group === "waiting";

  const entryPrice = Number(row?.entry_price ?? row?.entry);
  const currentPrice = Number(row?.current_price ?? row?.latest_close);
  const t1 = Number(row?.target_1 ?? row?.t1);
  const t2 = Number(row?.target_2 ?? row?.t2);
  const t3 = Number(row?.target_3 ?? row?.t3);

  if (!currentPrice || isNaN(currentPrice) || !entryPrice || isNaN(entryPrice)) {
    return {
      distanceRupees: null,
      distancePercent: null,
      targetName: null,
      targetPrice: null,
      className: "",
      style: {}
    };
  }

  if (isWaiting && currentPrice < entryPrice) {
    const distRs = Math.abs(entryPrice - currentPrice);
    const distPct = ((entryPrice - currentPrice) / entryPrice) * 100;
    const intensity = Math.min(Math.abs(distPct) / 10.0, 1.0);
    return {
      distanceRupees: distRs,
      distancePercent: distPct,
      targetName: "Entry",
      targetPrice: entryPrice,
      className: "paperDistanceRed",
      style: { "--distance-intensity": intensity.toFixed(3) }
    };
  }

  let nextTarget = null;
  let targetName = "Entry";

  if (currentPrice < entryPrice) {
    nextTarget = entryPrice;
    targetName = "Entry";
  } else if (t1 && !isNaN(t1) && currentPrice < t1) {
    nextTarget = t1;
    targetName = "T1";
  } else if (t2 && !isNaN(t2) && currentPrice < t2) {
    nextTarget = t2;
    targetName = "T2";
  } else if (t3 && !isNaN(t3) && currentPrice < t3) {
    nextTarget = t3;
    targetName = "T3";
  } else if (t3 && !isNaN(t3) && currentPrice >= t3) {
    nextTarget = t3;
    targetName = "T3";
  } else if (t2 && !isNaN(t2)) {
    nextTarget = t2;
    targetName = "T2";
  } else if (t1 && !isNaN(t1)) {
    nextTarget = t1;
    targetName = "T1";
  } else {
    nextTarget = entryPrice;
    targetName = "Entry";
  }

  if (currentPrice < entryPrice) {
    const distRs = entryPrice - currentPrice;
    const distPct = ((entryPrice - currentPrice) / entryPrice) * 100;
    const intensity = Math.min(Math.abs(distPct) / 10.0, 1.0);
    return {
      distanceRupees: distRs,
      distancePercent: distPct,
      targetName: "Entry",
      targetPrice: entryPrice,
      className: "paperDistanceRed",
      style: { "--distance-intensity": intensity.toFixed(3) }
    };
  }

  if (t3 && !isNaN(t3) && currentPrice >= t3 && nextTarget === t3) {
    return {
      distanceRupees: 0,
      distancePercent: 0,
      targetName: "T3",
      targetPrice: t3,
      className: "paperDistanceGreen",
      style: { "--distance-intensity": "0.000" }
    };
  }

  const distRs = Math.max(nextTarget - currentPrice, 0);
  const distPct = Math.max(((nextTarget - currentPrice) / nextTarget) * 100, 0);
  const intensity = Math.min(distPct / 10.0, 1.0);

  return {
    distanceRupees: distRs,
    distancePercent: distPct,
    targetName: targetName,
    targetPrice: nextTarget,
    className: "paperDistanceGreen",
    style: { "--distance-intensity": intensity.toFixed(3) }
  };
}

function getPaperDistanceStyle(row) {
  return getActiveTradeDistanceInfo(row);
}
function paperSortValue(row, field) {
  if (field === "strategy") return paperStrategyText(row);
  if (field === "status") return paperDisplayStatus(row);
  if (field === "symbol") return row?.symbol || row?.tradingview_symbol || row?.canonical_symbol || "";
  if (field === "setup_time") return row?.setup_time ?? row?.created_at ?? row?.source_confirmation_created_at ?? "";
  if (field === "pnl") return row?.paper_pnl ?? row?.pnl ?? row?.pnl_display ?? "";
  if (field === "t1") return row?.target_1 ?? row?.t1 ?? "";
  return paperCellValue(row, { key: field });
}
function comparePaperSortValues(a, b, field) {
  if (field === "setup_time") {
    const aTime = Date.parse(a);
    const bTime = Date.parse(b);
    const safeATime = Number.isFinite(aTime) ? aTime : 0;
    const safeBTime = Number.isFinite(bTime) ? bTime : 0;
    return safeATime - safeBTime;
  }
  const aNumber = numericSortValue(a);
  const bNumber = numericSortValue(b);
  if (aNumber !== null || bNumber !== null) return (aNumber ?? Number.NEGATIVE_INFINITY) - (bNumber ?? Number.NEGATIVE_INFINITY);
  return String(a ?? "").localeCompare(String(b ?? ""), undefined, { numeric: true, sensitivity: "base" });
}
function sortPaperTrades(rows, sortField = "setup_time", sortDirection = "desc") {
  const direction = sortDirection === "asc" ? 1 : -1;
  return (Array.isArray(rows) ? rows : [])
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const primary = comparePaperSortValues(paperSortValue(a.row, sortField), paperSortValue(b.row, sortField), sortField);
      if (primary) return primary * direction;
      const symbolCompare = comparePaperSortValues(paperSortValue(a.row, "symbol"), paperSortValue(b.row, "symbol"), "symbol");
      return symbolCompare || a.index - b.index;
    })
    .map((entry) => entry.row);
}
function paperPnlClass(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) return "neutral";
  return numeric > 0 ? "positive" : "negative";
}
function withPaperGroup(rows, group) {
  return (Array.isArray(rows) ? rows : []).map((trade) => ({ ...trade, paper_group: group }));
}

const PNL_CALENDAR_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const PNL_CALENDAR_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const PNL_CALENDAR_REALIZED_GROUPS = new Set(["completed", "stopped", "ambiguous"]);
const PNL_CALENDAR_EXTRA_REALIZED_STATUSES = new Set(["EXITED", "MANUAL_EXIT", "FORCED_EXIT"]);

function parsePaperNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const clean = String(value).replace(/[,₹%\s]/g, "");
  if (!clean) return null;
  const parsed = Number(clean);
  return Number.isFinite(parsed) ? parsed : null;
}
function getTradeRealizedPnl(trade) {
  const candidates = [
    trade?.realized_pnl,
    trade?.paper_pnl,
    trade?.total_pnl,
    trade?.total_trade_pnl,
    trade?.pnl,
    trade?.pnl_display,
  ];
  for (const candidate of candidates) {
    const parsed = parsePaperNumber(candidate);
    if (parsed !== null) return parsed;
  }
  return null;
}
function parseTradeDate(value) {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}
function getTradeExitDate(trade) {
  const candidates = [
    trade?.exit_date,
    trade?.completed_at,
    trade?.updated_at,
    trade?.setup_date,
    trade?.setup_time,
    trade?.created_at,
    trade?.source_confirmation_created_at,
  ];
  for (const candidate of candidates) {
    const date = parseTradeDate(candidate);
    if (date) return date;
  }
  return null;
}
function formatDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
function displayDate(date) {
  return date.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}
function isAmbiguousPnlTrade(trade) {
  return trade?.paper_group === "ambiguous" || hasAnyPaperStatus(paperStatusSet(trade), PAPER_AMBIGUOUS_STATUSES);
}
function isRealizedPnlTrade(trade) {
  const statuses = paperStatusSet(trade);
  const terminal = hasAnyPaperStatus(statuses, PAPER_COMPLETED_STATUSES)
    || hasAnyPaperStatus(statuses, PAPER_STOPPED_STATUSES)
    || hasAnyPaperStatus(statuses, PAPER_AMBIGUOUS_STATUSES)
    || hasAnyPaperStatus(statuses, PNL_CALENDAR_EXTRA_REALIZED_STATUSES)
    || PNL_CALENDAR_REALIZED_GROUPS.has(trade?.paper_group);
  const unresolved = hasAnyPaperStatus(statuses, PAPER_WAITING_STATUSES)
    || hasAnyPaperStatus(statuses, PAPER_ACTIVE_STATUSES)
    || hasAnyPaperStatus(statuses, PAPER_PARTIAL_STATUSES);
  const expiredOnly = hasAnyPaperStatus(statuses, PAPER_EXPIRED_STATUSES)
    && !hasAnyPaperStatus(statuses, PAPER_COMPLETED_STATUSES)
    && !hasAnyPaperStatus(statuses, PAPER_STOPPED_STATUSES)
    && !hasAnyPaperStatus(statuses, PAPER_AMBIGUOUS_STATUSES);
  return terminal && !unresolved && !expiredOnly;
}
function strategyBreakdownLabel(breakdown) {
  const entries = Object.entries(breakdown || {}).filter(([, item]) => item.count > 0);
  if (!entries.length) return "No strategy trades";
  return entries.map(([strategy, item]) => `${strategy}: ${item.count} (${money(item.pnl)})`).join(", ");
}
function buildDailyPnlMap(trades, year, strategyFilter = "ALL") {
  const dailyMap = new Map();
  const cleanFilter = String(strategyFilter || "ALL").toUpperCase();
  for (const trade of Array.isArray(trades) ? trades : []) {
    if (!isRealizedPnlTrade(trade)) continue;
    const strategy = paperStrategyText(trade);
    if (cleanFilter !== "ALL" && !strategy.toUpperCase().includes(cleanFilter)) continue;
    const pnl = getTradeRealizedPnl(trade);
    const date = getTradeExitDate(trade);
    if (pnl === null || !date || date.getFullYear() !== Number(year)) continue;
    const dateKey = formatDateKey(date);
    const day = dailyMap.get(dateKey) || {
      date,
      dateKey,
      totalPnl: 0,
      trades: 0,
      wins: 0,
      losses: 0,
      ambiguous: 0,
      bestTrade: null,
      worstTrade: null,
      strategyBreakdown: {},
    };
    day.totalPnl += pnl;
    day.trades += 1;
    if (pnl > 0) day.wins += 1;
    if (pnl < 0) day.losses += 1;
    if (isAmbiguousPnlTrade(trade)) day.ambiguous += 1;
    const tradeSummary = { symbol: trade?.symbol || trade?.tradingview_symbol || "-", pnl };
    if (!day.bestTrade || pnl > day.bestTrade.pnl) day.bestTrade = tradeSummary;
    if (!day.worstTrade || pnl < day.worstTrade.pnl) day.worstTrade = tradeSummary;
    const strategyEntry = day.strategyBreakdown[strategy] || { count: 0, pnl: 0 };
    strategyEntry.count += 1;
    strategyEntry.pnl += pnl;
    day.strategyBreakdown[strategy] = strategyEntry;
    dailyMap.set(dateKey, day);
  }
  return dailyMap;
}

function buildDailyTargetExitMap(trades, year, strategyFilter = "ALL") {
  const dailyMap = new Map();
  const cleanFilter = String(strategyFilter || "ALL").toUpperCase();
  for (const trade of Array.isArray(trades) ? trades : []) {
    const status = trade?.status;
    const exReason = trade?.exit_reason;
    const rejReason = trade?.rejection_reason;
    if (status === "INVALIDATED_STALE" || exReason === "INVALIDATED_STALE" || rejReason === "INVALIDATED_STALE") {
      continue;
    }
    const strategy = paperStrategyText(trade);
    if (cleanFilter !== "ALL" && !strategy.toUpperCase().includes(cleanFilter)) continue;

    for (const key of ["partial_exit_1", "partial_exit_2", "partial_exit_3"]) {
      const pe = trade?.[key];
      if (pe && typeof pe === "object") {
        const date = parseTradeDate(pe.exited_at || pe.timestamp);
        if (!date || date.getFullYear() !== Number(year)) continue;
        const pnl = parsePaperNumber(pe.paper_pnl ?? pe.realized_pnl ?? pe.pnl);
        const qty = parsePaperNumber(pe.quantity ?? pe.qty);
        if (qty <= 0 && (pnl === null || pnl <= 0)) continue;

        const dateKey = formatDateKey(date);
        const day = dailyMap.get(dateKey) || {
          date,
          dateKey,
          totalPnl: 0,
          trades: 0,
          wins: 0,
          losses: 0,
          ambiguous: 0,
          bestTrade: null,
          worstTrade: null,
          strategyBreakdown: {},
        };
        const pnlVal = pnl ?? 0;
        day.totalPnl += pnlVal;
        day.trades += 1;
        if (pnlVal > 0) day.wins += 1;
        if (pnlVal < 0) day.losses += 1;

        const stage = pe.exit_stage || key.replace("partial_exit_", "T").toUpperCase();
        const tradeSummary = {
          symbol: `${trade?.symbol || trade?.tradingview_symbol || "-"} (${stage})`,
          pnl: pnlVal,
        };
        if (!day.bestTrade || pnlVal > day.bestTrade.pnl) day.bestTrade = tradeSummary;
        if (!day.worstTrade || pnlVal < day.worstTrade.pnl) day.worstTrade = tradeSummary;

        const strategyEntry = day.strategyBreakdown[strategy] || { count: 0, pnl: 0 };
        strategyEntry.count += 1;
        strategyEntry.pnl += pnlVal;
        day.strategyBreakdown[strategy] = strategyEntry;

        dailyMap.set(dateKey, day);
      }
    }
  }
  return dailyMap;
}

function buildMonthlyPnlSummary(dailyMap, year) {
  const months = PNL_CALENDAR_MONTHS.map((monthName, month) => ({
    month,
    monthName,
    totalPnl: 0,
    totalTrades: 0,
    winningDays: 0,
    losingDays: 0,
    bestDay: null,
    worstDay: null,
  }));
  for (const day of dailyMap.values()) {
    const month = day.date.getMonth();
    if (day.date.getFullYear() !== Number(year) || !months[month]) continue;
    const item = months[month];
    item.totalPnl += day.totalPnl;
    item.totalTrades += day.trades;
    if (day.totalPnl > 0) item.winningDays += 1;
    if (day.totalPnl < 0) item.losingDays += 1;
    if (!item.bestDay || day.totalPnl > item.bestDay.totalPnl) item.bestDay = day;
    if (!item.worstDay || day.totalPnl < item.worstDay.totalPnl) item.worstDay = day;
  }
  return months;
}
function getPnlIntensityClass(value, maxAbsPnl) {
  if (!Number.isFinite(value) || value === 0) return "pnlDayNeutral";
  const ratio = Math.abs(value) / Math.max(Math.abs(maxAbsPnl || 0), 1);
  const level = ratio >= 0.75 ? 4 : ratio >= 0.5 ? 3 : ratio >= 0.25 ? 2 : 1;
  return value > 0 ? `pnlDayProfit${level}` : `pnlDayLoss${level}`;
}
function getTradeCountIntensityClass(count, maxCount) {
  if (!Number.isFinite(count) || count <= 0) return "pnlDayEmpty";
  const ratio = count / Math.max(maxCount || 0, 1);
  const level = ratio >= 0.75 ? 4 : ratio >= 0.5 ? 3 : ratio >= 0.25 ? 2 : 1;
  return `pnlDayCount${level}`;
}
function buildCalendarCells(year) {
  const first = new Date(Number(year), 0, 1);
  const start = new Date(first);
  start.setDate(first.getDate() - first.getDay());
  const last = new Date(Number(year), 11, 31);
  const end = new Date(last);
  end.setDate(last.getDate() + (6 - last.getDay()));
  const cells = [];
  const cursor = new Date(start);
  while (cursor <= end) {
    cells.push({
      date: new Date(cursor),
      dateKey: formatDateKey(cursor),
      inYear: cursor.getFullYear() === Number(year),
      week: Math.floor((cells.length || 0) / 7),
      weekday: cursor.getDay(),
    });
    cursor.setDate(cursor.getDate() + 1);
  }
  return cells;
}
function buildMonthLabels(year, cells) {
  return PNL_CALENDAR_MONTHS.map((monthName, month) => {
    const firstKey = formatDateKey(new Date(Number(year), month, 1));
    const nextKey = month < 11 ? formatDateKey(new Date(Number(year), month + 1, 1)) : null;
    const firstCell = cells.find((cell) => cell.dateKey === firstKey);
    const nextCell = nextKey ? cells.find((cell) => cell.dateKey === nextKey) : null;
    const startWeek = firstCell?.week ?? 0;
    const endWeek = nextCell?.week ?? (cells[cells.length - 1]?.week ?? startWeek) + 1;
    return { monthName, startWeek, span: Math.max(1, endWeek - startWeek) };
  });
}
function PnlCalendarCell({ cell, day, metric, maxAbsPnl, maxTradeCount }) {
  const valueClass = day
    ? metric === "count"
      ? getTradeCountIntensityClass(day.trades, maxTradeCount)
      : getPnlIntensityClass(day.totalPnl, maxAbsPnl)
    : "pnlDayEmpty";
  const className = [
    "pnlDayCell",
    valueClass,
    cell.inYear ? "" : "pnlDayOutside",
  ].filter(Boolean).join(" ");
  const title = day
    ? [
      displayDate(day.date),
      `P&L: ${money(day.totalPnl)}`,
      `Events / Trades: ${day.trades}`,
      `Wins: ${day.wins}`,
      `Losses: ${day.losses}`,
      `Ambiguous: ${day.ambiguous}`,
      `Strategy: ${strategyBreakdownLabel(day.strategyBreakdown)}`,
      `Best: ${day.bestTrade?.symbol || "-"} ${money(day.bestTrade?.pnl ?? 0)}`,
      `Worst: ${day.worstTrade?.symbol || "-"} ${money(day.worstTrade?.pnl ?? 0)}`,
    ].join("\n")
    : `${displayDate(cell.date)}\nNo realized P&L events`;
  return (
    <div
      aria-label={title.replace(/\n/g, ". ")}
      className={className}
      role="gridcell"
      style={{ gridColumn: cell.week + 1, gridRow: cell.weekday + 1 }}
      title={title}
    />
  );
}
function MonthlyPnlSummary({ months }) {
  const maxAbsPnl = Math.max(...months.map((month) => Math.abs(month.totalPnl)), 1);
  return <div className="monthlyPnlGrid">
    {months.map((month) => {
      const tone = month.totalTrades === 0 ? "empty" : month.totalPnl > 0 ? "profit" : month.totalPnl < 0 ? "loss" : "flat";
      const width = `${Math.max(4, Math.round((Math.abs(month.totalPnl) / maxAbsPnl) * 100))}%`;
      const title = [
        month.monthName,
        `P&L: ${money(month.totalPnl)}`,
        `Events / Trades: ${month.totalTrades}`,
        `Winning days: ${month.winningDays}`,
        `Losing days: ${month.losingDays}`,
        `Best day: ${month.bestDay ? `${displayDate(month.bestDay.date)} ${money(month.bestDay.totalPnl)}` : "-"}`,
        `Worst day: ${month.worstDay ? `${displayDate(month.worstDay.date)} ${money(month.worstDay.totalPnl)}` : "-"}`,
      ].join("\n");
      return <div className={`monthlyPnlCard monthlyPnl-${tone}`} key={month.monthName} title={title}>
        <span>{month.monthName}</span>
        <strong>{money(month.totalPnl)}</strong>
        <p>{month.totalTrades} events / trades</p>
        <small>{month.winningDays} win days / {month.losingDays} loss days</small>
        <div className="monthlyPnlBar"><i style={{ width }} /></div>
      </div>;
    })}
  </div>;
}
function PnlCalendarHeatmap({ trades = [] }) {
  const availableYears = useMemo(() => {
    const years = new Set();
    for (const trade of Array.isArray(trades) ? trades : []) {
      if (isRealizedPnlTrade(trade) && getTradeRealizedPnl(trade) !== null) {
        const date = getTradeExitDate(trade);
        if (date) years.add(date.getFullYear());
      }
      for (const key of ["partial_exit_1", "partial_exit_2", "partial_exit_3"]) {
        const pe = trade?.[key];
        if (pe && typeof pe === "object") {
          const d = parseTradeDate(pe.exited_at || pe.timestamp);
          if (d) years.add(d.getFullYear());
        }
      }
    }
    return [...years].sort((a, b) => b - a);
  }, [trades]);
  const [manualYear, setManualYear] = useState("");
  const [strategyFilter, setStrategyFilter] = useState("ALL");
  const [metric, setMetric] = useState("targets");
  const selectedYear = Number(manualYear || availableYears[0] || new Date().getFullYear());
  const yearOptions = availableYears.includes(selectedYear) ? availableYears : [selectedYear, ...availableYears];

  const closedDailyMap = useMemo(() => buildDailyPnlMap(trades, selectedYear, strategyFilter), [trades, selectedYear, strategyFilter]);
  const targetExitDailyMap = useMemo(() => buildDailyTargetExitMap(trades, selectedYear, strategyFilter), [trades, selectedYear, strategyFilter]);

  const activeDailyMap = metric === "closed" ? closedDailyMap : metric === "targets" ? targetExitDailyMap : targetExitDailyMap;

  const months = useMemo(() => buildMonthlyPnlSummary(activeDailyMap, selectedYear), [activeDailyMap, selectedYear]);
  const cells = useMemo(() => buildCalendarCells(selectedYear), [selectedYear]);
  const monthLabels = useMemo(() => buildMonthLabels(selectedYear, cells), [selectedYear, cells]);
  const maxAbsPnl = Math.max(...[...activeDailyMap.values()].map((day) => Math.abs(day.totalPnl)), 1);
  const maxTradeCount = Math.max(...[...activeDailyMap.values()].map((day) => day.trades), 1);
  const gridStyle = { gridTemplateColumns: `repeat(${Math.max(...cells.map((cell) => cell.week), 0) + 1}, 12px)` };

  const totalClosedTrades = [...closedDailyMap.values()].reduce((sum, day) => sum + day.trades, 0);
  const totalClosedPnl = [...closedDailyMap.values()].reduce((sum, day) => sum + day.totalPnl, 0);

  const totalTargetEvents = [...targetExitDailyMap.values()].reduce((sum, day) => sum + day.trades, 0);
  const totalTargetPnl = [...targetExitDailyMap.values()].reduce((sum, day) => sum + day.totalPnl, 0);

  return <section className="card pnlCalendarPanel">
    <div className="pnlCalendarHeader">
      <div>
        <span>analytics</span>
        <h2>P&L Calendar</h2>
        <p>Day-wise realized paper P&L &amp; target exit events</p>
      </div>
      <div className="pnlCalendarControls">
        <label>Year<select value={selectedYear} onChange={(event) => setManualYear(event.target.value)}>
          {yearOptions.map((year) => <option key={year} value={year}>{year}</option>)}
        </select></label>
        <label>Strategy<select value={strategyFilter} onChange={(event) => setStrategyFilter(event.target.value)}>
          <option value="ALL">All</option>
          <option value="SWING">Swing</option>
          <option value="MOMENTUM">Momentum</option>
        </select></label>
        <div className="pnlMetricToggle" role="group" aria-label="P&L calendar metric">
          <button className={metric === "targets" ? "active" : ""} type="button" onClick={() => setMetric("targets")}>Target Exits P&amp;L</button>
          <button className={metric === "closed" ? "active" : ""} type="button" onClick={() => setMetric("closed")}>Closed Trades P&amp;L</button>
          <button className={metric === "count" ? "active" : ""} type="button" onClick={() => setMetric("count")}>Trade Count</button>
        </div>
      </div>
    </div>
    <div className="pnlCalendarTotals" style={{ display: "flex", gap: "24px", alignItems: "center" }}>
      <div>
        <span style={{ fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px", opacity: 0.7, display: "block" }}>Closed Parent Trades</span>
        <span>{totalClosedTrades} closed trades</span> &bull; <strong className={paperPnlClass(totalClosedPnl)}>{money(totalClosedPnl)}</strong>
      </div>
      <div style={{ width: "1px", height: "30px", background: "rgba(255,255,255,0.15)" }} />
      <div>
        <span style={{ fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px", color: "var(--accent-color, #38bdf8)", fontWeight: 600, display: "block" }}>Realized Target Exit Events</span>
        <span>{totalTargetEvents} target exits</span> &bull; <strong className={paperPnlClass(totalTargetPnl)}>{money(totalTargetPnl)}</strong>
      </div>
    </div>
    <div className="pnlHeatmap" aria-label={`Daily realized paper P&L for ${selectedYear}`}>
      <div className="pnlHeatmapScroll">
        <div className="pnlMonthLabels" style={gridStyle}>
          {monthLabels.map((month) => <span key={month.monthName} style={{ gridColumn: `${month.startWeek + 1} / span ${month.span}` }}>{month.monthName}</span>)}
        </div>
        <div className="pnlHeatmapBody">
          <div className="pnlWeekdayLabels">{PNL_CALENDAR_WEEKDAYS.map((day) => <span key={day}>{day}</span>)}</div>
          <div className="pnlHeatmapGrid" role="grid" style={gridStyle}>
            {cells.map((cell) => <PnlCalendarCell key={cell.dateKey} cell={cell} day={activeDailyMap.get(cell.dateKey)} metric={metric} maxAbsPnl={maxAbsPnl} maxTradeCount={maxTradeCount} />)}
          </div>
        </div>
      </div>
    </div>
    <MonthlyPnlSummary months={months} />
  </section>;
}

function PaperTradeTable({ activeTab = "waiting", rows = [], loading = false, emptyMessage = "No trades match filters.", onShowProgress, onVerifyTrade }) {
  const columns = PAPER_TABLE_COLUMNS.filter((col) => {
    if (activeTab === "waiting") {
      return !["pnl", "reserved_margin"].includes(col.key);
    }
    if (activeTab === "completed") {
      return !["distance_to_entry", "distance_to_entry_percent"].includes(col.key);
    }
    return true;
  });

  if (loading) {
    return <div className="paperTableLoading"><span className="spinner" /> Loading paper trades...</div>;
  }
  return (
    <div className="tableShell">
      <table className="paperCompactTable">
        <thead>
          <tr>{columns.map((column) => <th key={column.key} className={column.key === "actions" ? "verifyTableCell" : ""}>{column.label}</th>)}</tr>
        </thead>
        <tbody>
            {rows.length ? rows.map((row, index) => <tr key={paperTradeIdentity(row, index)}>{columns.map((column) => {
              const value = paperCellValue(row, column);
              if (column.type === "strategy") return <td key={column.key}><Badge tone={paperStrategyTone(row)}>{paperStrategyText(row)}</Badge></td>;
              if (column.type === "status") {
                const isHistorical = Boolean(row?.historical_dataset_mode);
                return (
                  <td key={column.key}>
                    <div style={{ display: "inline-flex", alignItems: "center", gap: "4px", flexWrap: "wrap" }}>
                      <Badge tone={paperStatusToneFromLabel(value)}>{val(value)}</Badge>
                      {isHistorical && <Badge tone="gray">HISTORICAL REPLAY</Badge>}
                    </div>
                  </td>
                );
              }
              if (column.type === "pnl") return <td key={column.key}><span className={`paperPnl ${paperPnlClass(value)}`}>{fmt(value)}</span></td>;
              if (column.key === "distance_to_entry" || column.key === "distance_to_entry_percent") {
                const distInfo = getPaperDistanceStyle(row);
                return (
                  <td key={column.key}>
                    {value !== null && value !== undefined && value !== "—" ? (
                      <span className={distInfo.className} style={distInfo.style}>
                        {fmt(value)}
                      </span>
                    ) : (
                      fmt(value)
                    )}
                  </td>
                );
              }
              if (column.type === "shares") {
                const normStatus = normalizePaperStatus(row?.status);
                const warning = row?.quantity_integrity_warning;
                let text = "Unavailable";
                if (!warning && row?.bought_quantity !== null && row?.bought_quantity !== undefined) {
                  if (PAPER_EXPIRED_STATUSES.has(normStatus)) {
                    text = "0 bought / 0 open";
                  } else if (PAPER_WAITING_STATUSES.has(normStatus)) {
                    text = `0 bought / ${row.planned_quantity} planned`;
                  } else if (PAPER_ACTIVE_STATUSES.has(normStatus) || PAPER_PARTIAL_STATUSES.has(normStatus)) {
                    text = `${row.bought_quantity} bought / ${row.open_quantity} open`;
                  } else if (PAPER_COMPLETED_STATUSES.has(normStatus) || PAPER_STOPPED_STATUSES.has(normStatus)) {
                    text = `${row.bought_quantity} bought / 0 open`;
                  }
                }
                return <td key={column.key} className="sharesCell">{text}</td>;
              }
              if (column.type === "margin") {
                const isHistorical = Boolean(row?.historical_dataset_mode);
                return (
                  <td key={column.key} className="marginCell">
                    {money(value)}
                    {isHistorical && <span style={{ fontSize: "10px", opacity: 0.7, display: "block" }}>[Dataset Isolation]</span>}
                  </td>
                );
              }
              if (column.type === "price") {
                const normStatus = normalizePaperStatus(row?.status);
                if (PAPER_COMPLETED_STATUSES.has(normStatus) || PAPER_STOPPED_STATUSES.has(normStatus)) {
                  return (
                    <td key={column.key} className="priceCell priceExit">
                      <span className="priceLabel textMuted" style={{ fontSize: "10px", opacity: 0.7, marginRight: "4px" }}>Exit:</span>
                      <strong>{value !== null && value !== undefined ? fmt(value) : "—"}</strong>
                    </td>
                  );
                } else {
                  return (
                    <td key={column.key} className="priceCell priceCurrent">
                      <span className="priceLabel textMuted" style={{ fontSize: "10px", opacity: 0.7, marginRight: "4px" }}>Current:</span>
                      <strong>{value !== null && value !== undefined ? fmt(value) : "—"}</strong>
                    </td>
                  );
                }
              }
              if (column.type === "actions") {
                return (
                  <td key={column.key} className="actionsCell verifyTableCell">
                    <div style={{ display: "flex", gap: "6px" }}>
                      <button
                        type="button"
                        className="paperStateButton"
                        style={{ padding: "3px 8px", fontSize: "11px", height: "auto", minWidth: "60px" }}
                        onClick={() => onShowProgress && onShowProgress(row)}
                      >
                        Progress
                      </button>
                      <button
                        type="button"
                        className="paperStateButton"
                        style={{ padding: "3px 8px", fontSize: "11px", height: "auto", minWidth: "55px" }}
                        onClick={() => onVerifyTrade && onVerifyTrade(row)}
                      >
                        Verify
                      </button>
                    </div>
                  </td>
                );
              }
              return <td key={column.key}>{fmt(value)}</td>;
            })}</tr>) : <tr><td colSpan={columns.length}>{emptyMessage}</td></tr>}
          </tbody>
        </table>
      </div>
  );
}

function PaperTrades({ openTrades, history, summary, liveStatus }) {
  const [paperSearch, setPaperSearch] = useState("");
  const [strategyFilter, setStrategyFilter] = useState("ALL");
  const [activeTradeFilter, setActiveTradeFilter] = useState("waiting");
  const [sortField, setSortField] = useState("setup_time");
  const [sortDirection, setSortDirection] = useState("desc");
  const [selectedProgressTrade, setSelectedProgressTrade] = useState(null);

  const handleVerifyTrade = (trade) => {
    const url = buildTradingViewUrl(trade);
    if (url) {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  };

  const openTradeRows = Array.isArray(openTrades)
    ? openTrades
    : Array.isArray(openTrades?.trades)
      ? openTrades.trades
      : Array.isArray(openTrades?.results)
        ? openTrades.results
        : [...(Array.isArray(openTrades?.waiting_for_entry) ? openTrades.waiting_for_entry : []), ...(Array.isArray(openTrades?.active_partial) ? openTrades.active_partial : [])];
  const waitingTrades = withPaperGroup(openTradeRows.filter((t) => hasAnyPaperStatus(paperStatusSet(t), PAPER_WAITING_STATUSES)), "waiting");
  const activeTrades = withPaperGroup(openTradeRows.filter((t) => hasAnyPaperStatus(paperStatusSet(t), PAPER_ACTIVE_STATUSES) || hasAnyPaperStatus(paperStatusSet(t), PAPER_PARTIAL_STATUSES)), "active");
  const completedTrades = withPaperGroup(arr(history, ["completed"]), "completed");
  const stoppedTrades = withPaperGroup(arr(history, ["sl_hit"]), "stopped");
  const ambiguousTrades = withPaperGroup(arr(history, ["ambiguous"]), "ambiguous");
  const expiredTrades = withPaperGroup(arr(history, ["expired_not_triggered"]), "expired");
  const calendarTrades = dedupePaperTrades([...activeTrades, ...completedTrades, ...stoppedTrades, ...ambiguousTrades]);
  const allRows = dedupePaperTrades([...waitingTrades, ...activeTrades, ...completedTrades, ...stoppedTrades, ...ambiguousTrades, ...expiredTrades]);
  const selectedFilter = PAPER_TRADE_FILTERS.find((filter) => filter.key === activeTradeFilter) || PAPER_TRADE_FILTERS[0];
  const tabRows = selectedFilter.groups ? allRows.filter((trade) => selectedFilter.groups.has(trade.paper_group)) : allRows;
  let filteredRows = tabRows.filter((trade) => paperTradeMatchesFilters(trade, paperSearch, strategyFilter));
  filteredRows = sortPaperTrades(filteredRows, sortField, sortDirection);
  const initialLoading = liveStatus?.running && !liveStatus?.hasLoaded;
  const emptyMessage = liveStatus?.hasLoaded ? "No paper trades match the current filters." : "No paper trades loaded yet.";
  const waitingCount = summary?.waiting_for_entry ?? summary?.waiting_trades ?? openTrades?.waiting_count ?? waitingTrades.length;
  const activeCount = summary?.open_trades ?? openTrades?.active_partial_count ?? activeTrades.length;
  const targetCompletedCount = summary?.target_completed_count ?? summary?.target_hit_count ?? completedTrades.length;
  const targetPartialThenSlCount = summary?.target_partial_then_sl_count ?? stoppedTrades.filter((t) => t.partial_exit_1 || t.t1_hit).length;
  const pureSlCount = summary?.pure_sl_hit_count ?? summary?.sl_hit_count ?? stoppedTrades.filter((t) => !t.partial_exit_1 && !t.t1_hit && t.exit_reason !== "STOP_LOSS_HIT_BEFORE_ENTRY").length;

  return <div className="pageStack">
    <div className="topHeader">
      <div><h2>Paper Trades</h2><p>Live tracking and history of automated paper executions.</p></div>
    </div>
    <div className="pageContent pageStack">
      {liveStatus?.error && <div className="errorPanel">{liveStatus.error}</div>}
      <div className="heroGrid">
        <StatCard label="Waiting for Entry" value={waitingCount} tone="yellow" />
        <StatCard label="Active" value={activeCount} />
        <StatCard label="Target Completed" value={targetCompletedCount} tone="green" />
        <StatCard label="Target → SL" value={targetPartialThenSlCount} tone="yellow" />
        <StatCard label="Pure SL Hit" value={pureSlCount} tone="red" />
      </div>
      <PnlCalendarHeatmap trades={calendarTrades} />
      <div className="card paperTableCard">
        <div className="paperTradeToolbar">
          <div className="paperFilterButtons" role="group" aria-label="Paper trade status filter">
            {PAPER_TRADE_FILTERS.map((filter) => (
              <button key={filter.key} type="button" className={`paperStateButton ${activeTradeFilter === filter.key ? "active" : ""}`} onClick={() => setActiveTradeFilter(filter.key)}>
                {filter.label}
              </button>
            ))}
          </div>
          <input value={paperSearch} onChange={(event) => setPaperSearch(event.target.value)} placeholder="Search by symbol" className="searchInput" />
          <div style={{display:"flex", gap:"10px"}}>
            <select value={strategyFilter} onChange={(event) => setStrategyFilter(event.target.value)}>
              <option value="ALL">All strategies</option>
              <option value="SWING">Swing</option>
              <option value="MOMENTUM">Momentum</option>
            </select>
            <select value={`${sortField}|${sortDirection}`} onChange={(event) => {
              const [f, d] = event.target.value.split('|');
              setSortField(f);
              setSortDirection(d);
            }}>
              <option value="setup_time|desc">Newest setup first</option>
              <option value="setup_time|asc">Oldest setup first</option>
              <option value="pnl|desc">P&L high to low</option>
              <option value="pnl|asc">P&L low to high</option>
              <option value="t1|asc">T1 low to high</option>
              <option value="t1|desc">T1 high to low</option>
              <option value="symbol|asc">Symbol A-Z</option>
              <option value="strategy|asc">Strategy</option>
              <option value="status|asc">Status</option>
            </select>
          </div>
        </div>
        <div className="paperTableMeta">
          <span>{filteredRows.length} of {tabRows.length} shown &middot; Sorted by {(PAPER_SORT_LABELS[`${sortField}|${sortDirection}`] || "custom").toLowerCase()}</span>
          <Badge tone={selectedFilter.key === "completed" ? "yellow" : selectedFilter.key === "active" ? "green" : "gray"}>{selectedFilter.label}</Badge>
        </div>
        <div className="paperTableShell">
          <PaperTradeTable rows={filteredRows} activeTab={activeTradeFilter} loading={initialLoading} emptyMessage={emptyMessage} onShowProgress={(trade) => setSelectedProgressTrade(trade)} onVerifyTrade={handleVerifyTrade} />
        </div>
      </div>

      {selectedProgressTrade && (
        <div className="modalOverlay" onClick={() => setSelectedProgressTrade(null)}>
          <div className="modalCard progressModalCard" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <div>
                <h3>Trade Progress — {selectedProgressTrade.symbol}</h3>
                <div className="modalSubtitle">{paperStrategyText(selectedProgressTrade)} Strategy &middot; {paperDisplayStatus(selectedProgressTrade)}</div>
              </div>
              <button type="button" className="btnSecondary" onClick={() => setSelectedProgressTrade(null)}>Close</button>
            </div>
            <div className="progressSectionStack">
              <div className="progressSectionCard">
                <h4 className="progressSectionTitle">1. Trade Metadata &amp; Strategy</h4>
                <div className="progressGrid">
                  <div><span>Symbol</span><strong>{selectedProgressTrade.symbol ?? selectedProgressTrade.tradingview_symbol ?? "N/A"}</strong></div>
                  <div><span>Strategy</span><strong>{paperStrategyText(selectedProgressTrade)}</strong></div>
                  <div><span>Status</span><strong>{paperDisplayStatus(selectedProgressTrade)}</strong></div>
                  <div><span>Quality Grade</span><strong>{selectedProgressTrade.trade_quality_grade ?? "N/A"}</strong></div>
                  <div><span>Setup Time</span><strong>{paperCellValue(selectedProgressTrade, { key: "setup_time" })}</strong></div>
                  <div><span>Valid Until</span><strong>{paperCellValue(selectedProgressTrade, { key: "setup_valid_until" })}</strong></div>
                </div>
              </div>

              <div className="progressSectionCard">
                <h4 className="progressSectionTitle">2. Price Levels</h4>
                <div className="progressGrid">
                  <div><span>Entry Price</span><strong>₹{selectedProgressTrade.entry_price ?? selectedProgressTrade.entry ?? "—"}</strong></div>
                  <div><span>Current Price</span><strong>₹{selectedProgressTrade.current_price ?? selectedProgressTrade.latest_close ?? "—"}</strong></div>
                  <div><span>Stop Loss</span><strong>₹{selectedProgressTrade.stop_loss ?? selectedProgressTrade.current_stop_loss ?? "—"}</strong></div>
                  <div><span>Target 1 (T1)</span><strong>₹{selectedProgressTrade.target_1 ?? selectedProgressTrade.t1 ?? "—"}</strong></div>
                  <div><span>Target 2 (T2)</span><strong>{selectedProgressTrade.target_2 != null ? `₹${selectedProgressTrade.target_2}` : selectedProgressTrade.t2 != null ? `₹${selectedProgressTrade.t2}` : "N/A"}</strong></div>
                  <div><span>Target 3 (T3)</span><strong>{selectedProgressTrade.target_3 != null ? `₹${selectedProgressTrade.target_3}` : selectedProgressTrade.t3 != null ? `₹${selectedProgressTrade.t3}` : "N/A"}</strong></div>
                </div>
              </div>

              <div className="progressSectionCard">
                <h4 className="progressSectionTitle">3. Position Sizing &amp; Margin</h4>
                <div className="progressGrid">
                  <div><span>Planned Quantity</span><strong>{selectedProgressTrade.planned_quantity ?? selectedProgressTrade.quantity ?? "N/A"}</strong></div>
                  <div><span>Bought Quantity</span><strong>{selectedProgressTrade.bought_quantity ?? "N/A"}</strong></div>
                  <div><span>Open Quantity</span><strong>{selectedProgressTrade.open_quantity ?? "N/A"}</strong></div>
                  <div><span>Reserved Margin</span><strong>₹{selectedProgressTrade.reserved_margin ?? "—"}</strong></div>
                </div>
              </div>

              <div className="progressSectionCard">
                <h4 className="progressSectionTitle">4. Price Progress</h4>
                <div className="progressGrid">
                  <div><span>Distance to Entry (₹)</span><strong>₹{paperCellValue(selectedProgressTrade, { key: "distance_to_entry" })}</strong></div>
                  <div><span>Distance to Entry (%)</span><strong>{paperCellValue(selectedProgressTrade, { key: "distance_to_entry_percent" })}</strong></div>
                  <div><span>Distance to SL (₹)</span><strong>{(() => {
                    const cp = Number(selectedProgressTrade.current_price ?? selectedProgressTrade.latest_close);
                    const sl = Number(selectedProgressTrade.stop_loss ?? selectedProgressTrade.current_stop_loss);
                    if (!isNaN(cp) && !isNaN(sl)) {
                      return `₹${(cp - sl).toFixed(2)}`;
                    }
                    return "N/A";
                  })()}</strong></div>
                  <div><span>Distance to SL (%)</span><strong>{(() => {
                    const cp = Number(selectedProgressTrade.current_price ?? selectedProgressTrade.latest_close);
                    const sl = Number(selectedProgressTrade.stop_loss ?? selectedProgressTrade.current_stop_loss);
                    if (!isNaN(cp) && !isNaN(sl) && cp > 0) {
                      return `${(((cp - sl) / cp) * 100).toFixed(2)}%`;
                    }
                    return "N/A";
                  })()}</strong></div>
                  <div><span>P&amp;L</span><strong>₹{paperCellValue(selectedProgressTrade, { key: "pnl" })}</strong></div>
                </div>
              </div>

              <div className="progressSectionCard">
                <h4 className="progressSectionTitle">5. Live Trade Progress</h4>
                {selectedProgressTrade.status === "WAITING_FOR_ENTRY" || selectedProgressTrade.status === "WAITING_FOR_CAPITAL" ? (
                  <p className="muted progressNotice">Position not triggered yet. Live tracking active once trade triggers.</p>
                ) : (
                  <div className="progressGrid">
                    <div><span>Max High</span><strong>{selectedProgressTrade.max_high != null ? `₹${selectedProgressTrade.max_high}` : "N/A"}</strong></div>
                    <div><span>Min Low</span><strong>{selectedProgressTrade.min_low != null ? `₹${selectedProgressTrade.min_low}` : "N/A"}</strong></div>
                    <div><span>Current R</span><strong>{(() => {
                      if (selectedProgressTrade.current_r != null) return `${selectedProgressTrade.current_r} R`;
                      const cp = Number(selectedProgressTrade.current_price ?? selectedProgressTrade.latest_close);
                      const entry = Number(selectedProgressTrade.entry_price ?? selectedProgressTrade.entry);
                      const sl = Number(selectedProgressTrade.stop_loss ?? selectedProgressTrade.current_stop_loss);
                      const isTriggered = Boolean(selectedProgressTrade.entry_triggered_at) || !["WAITING_FOR_ENTRY", "WAITING_FOR_CAPITAL"].includes(selectedProgressTrade.status);
                      if (isTriggered && !isNaN(cp) && !isNaN(entry) && !isNaN(sl) && entry > sl) {
                        return `${((cp - entry) / (entry - sl)).toFixed(4)} R`;
                      }
                      return "N/A";
                    })()}</strong></div>
                    <div><span>Realized RR</span><strong>{selectedProgressTrade.realized_rr != null ? `${selectedProgressTrade.realized_rr} R` : "N/A"}</strong></div>
                  </div>
                )}
              </div>

              <div className="progressSectionCard">
                <h4 className="progressSectionTitle">6. Execution Logic &amp; Diagnostics</h4>
                <div className="progressGrid">
                  <div><span>EMA Alignment</span><strong>{selectedProgressTrade.ema_alignment ?? "N/A"}</strong></div>
                  <div><span>ATR</span><strong>{selectedProgressTrade.atr ?? "N/A"}</strong></div>
                  <div><span>Volume Confirmation</span><strong>{selectedProgressTrade.volume_confirmation ?? "N/A"}</strong></div>
                  <div><span>MTF Confirmation</span><strong>{selectedProgressTrade.mtf_confirmation ?? "N/A"}</strong></div>
                  <div><span>Trap Detection</span><strong>{selectedProgressTrade.trap_status ?? "N/A"}</strong></div>
                </div>
              </div>

              {/* SECTION 7 — LIVE TRADE PROGRESS & OUTCOME */}
              <div className="progressSectionCard sec7LiveCard">
                <h4 className="progressSectionTitle">7. Live Trade Progress &amp; Outcome</h4>
                
                {/* A. TOP STATUS HEADER */}
                <div className="sec7Header">
                  <div>
                    <div className="sec7HeaderLabel">Trade Status</div>
                    <Badge tone={paperStatusToneFromLabel(paperDisplayStatus(selectedProgressTrade))}>
                      {paperDisplayStatus(selectedProgressTrade)}
                    </Badge>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <div className="sec7HeaderLabel">Current Market Price</div>
                    <div className="sec7PriceBig">
                      {selectedProgressTrade.current_price ?? selectedProgressTrade.latest_close ? `₹${selectedProgressTrade.current_price ?? selectedProgressTrade.latest_close}` : "--"}
                    </div>
                  </div>
                </div>

                {/* B. PRICE PROGRESSION BAR */}
                {(() => {
                  const cp = Number(selectedProgressTrade.current_price ?? selectedProgressTrade.latest_close);
                  const sl = Number(selectedProgressTrade.stop_loss ?? selectedProgressTrade.current_stop_loss);
                  const entry = Number(selectedProgressTrade.entry_price ?? selectedProgressTrade.entry);
                  const t1 = Number(selectedProgressTrade.target_1 ?? selectedProgressTrade.t1);
                  const t2 = Number(selectedProgressTrade.target_2 ?? selectedProgressTrade.t2);
                  const t3 = Number(selectedProgressTrade.target_3 ?? selectedProgressTrade.t3);

                  let pct = 25; // default at Entry
                  if (!isNaN(cp) && !isNaN(sl) && !isNaN(entry)) {
                    if (cp <= sl) {
                      pct = 0;
                    } else if (cp < entry) {
                      const range = entry - sl;
                      pct = range > 0 ? Math.min(25, Math.max(0, ((cp - sl) / range) * 25)) : 25;
                    } else if (!isNaN(t1) && cp < t1) {
                      const range = t1 - entry;
                      pct = range > 0 ? Math.min(50, Math.max(25, 25 + ((cp - entry) / range) * 25)) : 25;
                    } else if (!isNaN(t2) && cp < t2) {
                      const range = t2 - t1;
                      pct = range > 0 ? Math.min(75, Math.max(50, 50 + ((cp - t1) / range) * 25)) : 50;
                    } else if (!isNaN(t3) && cp < t3) {
                      const range = t3 - t2;
                      pct = range > 0 ? Math.min(100, Math.max(75, 75 + ((cp - t2) / range) * 25)) : 75;
                    } else if (!isNaN(t3) && cp >= t3) {
                      pct = 100;
                    } else if (!isNaN(t2) && cp >= t2) {
                      pct = 75;
                    } else if (!isNaN(t1) && cp >= t1) {
                      pct = 50;
                    } else if (cp >= entry) {
                      pct = 25;
                    }
                  }

                  const isEntryTriggered = Boolean(selectedProgressTrade.entry_triggered_at) || !["WAITING_FOR_ENTRY", "WAITING_FOR_CAPITAL"].includes(selectedProgressTrade.status);
                  const isWaitingState = ["WAITING_FOR_ENTRY", "WAITING_FOR_CAPITAL"].includes(selectedProgressTrade.status) || (!selectedProgressTrade.entry_triggered && !selectedProgressTrade.entry_triggered_at);
                  const maxH = selectedProgressTrade.max_high != null ? Number(selectedProgressTrade.max_high) : null;
                  const minL = selectedProgressTrade.min_low != null ? Number(selectedProgressTrade.min_low) : null;

                  const isSlHit = selectedProgressTrade.status === "SL_HIT" || selectedProgressTrade.status === "STOPPED" || selectedProgressTrade.exit_reason === "STOP_LOSS_HIT";

                  return (
                    <>
                      <div className="sec7TrackContainer">
                        <div className="sec7TrackLabels">
                          <span><span>SL</span><strong>{sl ? `₹${sl}` : "--"}</strong></span>
                          <span><span>Entry</span><strong>{entry ? `₹${entry}` : "--"}</strong></span>
                          <span><span>T1</span><strong>{t1 ? `₹${t1}` : "--"}</strong></span>
                          <span><span>T2</span><strong>{t2 ? `₹${t2}` : "--"}</strong></span>
                          <span><span>T3</span><strong>{t3 ? `₹${t3}` : "--"}</strong></span>
                        </div>
                        <div className="sec7TrackBarWrapper">
                          <div className="sec7TrackFill" style={{ width: `${pct}%` }} />
                          <div className="sec7TrackMarker" style={{ left: `${pct}%` }} title={`Current: ₹${cp}`} />
                        </div>
                        <div className="sec7GainPill">
                          &bull; {isWaitingState ? `Distance to Entry (${!isNaN(cp) && !isNaN(entry) ? `₹${(entry - cp).toFixed(2)}` : "--"})` : `Active Price (${!isNaN(cp) ? `₹${cp}` : "--"})`}
                        </div>
                      </div>

                      {/* D. THREE-COLUMN TRADE PROGRESS CARDS */}
                      <div className="sec7CardGrid">
                        {/* 1. ENTRY */}
                        <div className="sec7CompactCard">
                          <div className="sec7CardTitle">1. Entry</div>
                          <div className="sec7CardMain">{isEntryTriggered ? "Triggered" : "Not Triggered"}</div>
                          <div className="sec7CardSub">{entry ? `₹${entry}` : "--"}</div>
                        </div>

                        {/* 2. MAX (HIGH) */}
                        <div className="sec7CompactCard">
                          <div className="sec7CardTitle">2. Max (High)</div>
                          <div className="sec7CardMain">{maxH != null ? `₹${maxH}` : "N/A"}</div>
                          <div className="sec7CardSub">
                            {maxH != null && entry ? `+₹${(maxH - entry).toFixed(2)} (+${(((maxH - entry) / entry) * 100).toFixed(2)}%)` : "No data"}
                          </div>
                        </div>

                        {/* 3. MIN (LOW) */}
                        <div className="sec7CompactCard">
                          <div className="sec7CardTitle">3. Min (Low)</div>
                          <div className="sec7CardMain">{minL != null ? `₹${minL}` : "N/A"}</div>
                          <div className="sec7CardSub">
                            {minL != null && entry ? `₹${(minL - entry).toFixed(2)} (${(((minL - entry) / entry) * 100).toFixed(2)}%)` : "No data"}
                          </div>
                        </div>

                        {/* 4. TARGET 1 */}
                        <div className="sec7CompactCard">
                          <div className="sec7CardTitle">4. Target 1</div>
                          <div className="sec7CardMain">
                            {!isNaN(cp) && !isNaN(t1) ? (cp >= t1 ? "Hit" : `₹${(t1 - cp).toFixed(2)} Away`) : "N/A"}
                          </div>
                          <div className="sec7CardSub">{t1 ? `Target: ₹${t1}` : "--"}</div>
                        </div>

                        {/* 5. TARGET 2 */}
                        <div className="sec7CompactCard">
                          <div className="sec7CardTitle">5. Target 2</div>
                          <div className="sec7CardMain">
                            {!isNaN(cp) && !isNaN(t2) ? (cp >= t2 ? "Hit" : `₹${(t2 - cp).toFixed(2)} Away`) : "N/A"}
                          </div>
                          <div className="sec7CardSub">{t2 ? `Target: ₹${t2}` : "--"}</div>
                        </div>

                        {/* 6. STOP LOSS */}
                        <div className="sec7CompactCard">
                          <div className="sec7CardTitle">6. Stop Loss</div>
                          <div className="sec7CardMain">{isSlHit ? "Hit" : "Not Hit"}</div>
                          <div className="sec7CardSub">{sl ? `Level: ₹${sl}` : "--"}</div>
                        </div>
                      </div>

                      {/* E. RISK / POSITION SUMMARY PANEL */}
                      <div className="sec7RiskPanel">
                        <div>
                          <div className="sec7RiskRow"><span>Risk Budget:</span><strong>{entry && sl && (selectedProgressTrade.planned_quantity || selectedProgressTrade.quantity) ? `₹${((entry - sl) * (selectedProgressTrade.planned_quantity || selectedProgressTrade.quantity)).toFixed(2)}` : "--"}</strong></div>
                          <div className="sec7RiskRow"><span>Distance to SL:</span><strong>{!isNaN(cp) && !isNaN(sl) ? `₹${(cp - sl).toFixed(2)} (${(((cp - sl) / cp) * 100).toFixed(2)}%)` : "--"}</strong></div>
                          <div className="sec7RiskRow"><span>Unrealized P&amp;L:</span><strong>₹{paperCellValue(selectedProgressTrade, { key: "pnl" })}</strong></div>
                        </div>
                        <div>
                          <div className="sec7RiskRow"><span>Position Size:</span><strong>{isWaitingState ? "0 shares" : `${selectedProgressTrade.open_quantity ?? selectedProgressTrade.bought_quantity ?? selectedProgressTrade.quantity ?? "--"} shares`}</strong></div>
                          <div className="sec7RiskRow"><span>Current R:</span><strong>{(() => {
                            if (selectedProgressTrade.current_r != null) return `${selectedProgressTrade.current_r} R`;
                            if (!isWaitingState && !isNaN(cp) && !isNaN(entry) && !isNaN(sl) && entry > sl) {
                              return `${((cp - entry) / (entry - sl)).toFixed(4)} R`;
                            }
                            return "N/A";
                          })()}</strong></div>
                          <div className="sec7RiskRow"><span>Reward Achieved:</span><strong>{selectedProgressTrade.realized_rr != null ? `${selectedProgressTrade.realized_rr} R` : "N/A"}</strong></div>
                        </div>
                      </div>

                      {/* F. OUTCOME MESSAGE & AUDIT */}
                      <div className="sec7OutcomeBox">
                        {selectedProgressTrade.exit_reason ? (
                          <div><strong>Outcome:</strong> {selectedProgressTrade.exit_reason}</div>
                        ) : selectedProgressTrade.rejection_reason || selectedProgressTrade.invalidation_reason || selectedProgressTrade.capital_rejection_reason ? (
                          <div>
                            {selectedProgressTrade.rejection_reason && <div><strong>Rejection Reason:</strong> {selectedProgressTrade.rejection_reason}</div>}
                            {selectedProgressTrade.invalidation_reason && <div><strong>Invalidation Reason:</strong> {selectedProgressTrade.invalidation_reason}</div>}
                            {selectedProgressTrade.capital_rejection_reason && <div><strong>Capital Rejection:</strong> {selectedProgressTrade.capital_rejection_reason}</div>}
                          </div>
                        ) : (
                          <div>No trade outcome available yet.</div>
                        )}
                      </div>
                    </>
                  );
                })()}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  </div>;
}
function Settings({ settings, health, runtimeInfo, tvRuntimeStatus, tvAttachableTabs, onRefreshTvTabs, onAttachTvTab, onDetachTvTab, loading }) {
  const gitCommit = runtimeInfo?.git_commit || "unknown";
  const startedAt = runtimeInfo?.started_at ? new Date(runtimeInfo.started_at).toLocaleString() : "unknown";
  const attached = tvRuntimeStatus?.attached_target;
  const attachableTargets = arr(tvAttachableTabs, ["targets"]);
  const tvStatusTone = attached?.target_id ? "green" : "yellow";
  return <div className="settingsPage">
    <Card title="Safety Locks" eyebrow="read-only">
      <div className="settingsGrid"><div><span>paper_only</span><strong>true</strong></div><div><span>live_trading</span><strong>false</strong></div><div><span>broker_orders</span><strong>false</strong></div><div><span>yfinance_for_tv_candles</span><strong>false</strong></div></div>
    </Card>
    <Card title="API Status" eyebrow="local backend">
      <div className="settingsGrid"><div><span>backend</span><strong>{health.status}</strong></div><div><span>API base</span><strong>{API_BASE}</strong></div><div><span>TradingView port</span><strong>{settings?.tradingview_debug_port ?? 9222}</strong></div><div><span>data source</span><strong>TradingView Desktop CDP</strong></div></div>
    </Card>
    <Card title="Running Project Identity" eyebrow="backend runtime">
      <div className="settingsGrid"><div><span>project_root</span><strong>{runtimeInfo?.project_root || "unknown"}</strong></div><div><span>backend_pid</span><strong>{runtimeInfo?.backend_pid ?? "unknown"}</strong></div><div><span>git_commit</span><strong>{gitCommit}</strong></div><div><span>started_at</span><strong>{startedAt}</strong></div></div>
    </Card>
    <Card title="TV Runtime" eyebrow="dedicated tab">
      <div className="tvAttachHeader">
        <Badge tone={tvStatusTone}>{attached?.target_id ? "attached" : "not attached"}</Badge>
        <div className="tvAttachActions">
          <ActionButton onClick={onRefreshTvTabs} disabled={loading}>Refresh TV Tabs</ActionButton>
          <ActionButton onClick={onDetachTvTab} disabled={loading || !attached?.target_id}>Detach</ActionButton>
        </div>
      </div>
      <div className="settingsGrid">
        <div><span>attached target</span><strong>{attached?.target_id || "none"}</strong></div>
        <div><span>title</span><strong>{attached?.title || "none"}</strong></div>
        <div><span>URL</span><strong>{attached?.url || "none"}</strong></div>
        <div><span>runtime</span><strong>{tvRuntimeStatus?.worker_running ? "busy" : tvRuntimeStatus?.connected ? "connected" : "idle"}</strong></div>
      </div>
      <div className="tvAttachList">
        {attachableTargets.length ? attachableTargets.map((target) => (
          <div className="tvAttachTarget" key={target.target_id}>
            <div><strong>{target.title || "TradingView chart"}</strong><span>{target.target_id}</span><span>{target.url}</span></div>
            <Badge tone={target.ready ? "green" : "yellow"}>{target.ready ? "ready" : "not ready"}</Badge>
            <ActionButton onClick={() => onAttachTvTab(target.target_id)} disabled={loading || attached?.target_id === target.target_id}>Attach</ActionButton>
          </div>
        )) : <p className="muted">No attachable TradingView chart tabs found.</p>}
      </div>
    </Card>
    <Card title="TradingView Desktop Reminder"><p className="muted">Keep TradingView Desktop running with debug port 9222, logged in, and one chart tab open. Attach that chart here before running TV confirmation.</p></Card>
  </div>;
}

function DataCollectionPage({ collectionStatus, summary }) {
  return <div className="pageStack">
    <div className="topHeader"><div><h2>Data Collection</h2><p>Read-only data collection status</p></div></div>
    <div className="pageContent pageStack">
      <Card title="Data Collection Pipeline" eyebrow="read-only tracking">
        <div className="datasetTrackingReminder">This page tracks data collection only. It does not generate predictions.</div>
        <div className="statsGrid compact aiDatasetBreakdowns">
          <StatCard label="Total Paper Trades" value={collectionStatus?.total_paper_trades ?? "--"} />
          <StatCard label="Terminal Paper Trades" value={collectionStatus?.terminal_paper_trades ?? "--"} />
          <StatCard label="Waiting Paper Trades" value={collectionStatus?.waiting_paper_trades ?? "--"} />
          <StatCard label="Open Paper Trades" value={collectionStatus?.open_paper_trades ?? "--"} />
        </div>
        <div className="datasetTrackingReminder" style={{ marginTop: '15px' }}>
          Terminal trades missing AI snapshots: {collectionStatus?.terminal_trades_without_ai_snapshot_symbols?.join(", ") || "none"}
        </div>
      </Card>
      <Card title="Historical Market Data" eyebrow="read-only coverage">
        <div className="statsGrid compact">
          <StatCard label="OHLCV Checkpoint" value="Tracking active" tone="green" />
          <StatCard label="Candidates Scored" value="Available" tone="green" />
        </div>
      </Card>
    </div>
  </div>;
}

function AiDatasetPage({ summary, snapshots, outcomePreview, filters, onFiltersChange, onRefresh, loading, errors }) {
  return <div className="pageStack">
    <div className="topHeader"><div><h2>AI Dataset / Labels</h2><p>Read-only dataset readiness</p></div></div>
    <div className="pageContent pageStack">
      <AiDatasetSummary summary={summary} snapshots={snapshots} outcomePreview={outcomePreview} filters={filters} onFiltersChange={onFiltersChange} onRefresh={onRefresh} loading={loading} errors={errors} />
    </div>
  </div>;
}

function SystemHealthPage({ health, tvRuntimeStatus, schedulerStatus, runs }) {
  return <div className="pageStack">
    <div className="topHeader"><div><h2>System Health</h2><p>Backend API, TV, and Automation Status</p></div></div>
    <div className="pageContent pageStack">
      <DashboardHealthPanel health={health} tradingViewStatus={tvRuntimeStatus} schedulerStatus={schedulerStatus} />
      <PaperAutomationStatus scheduler={schedulerStatus} />
      <PaperUpdateRunHistory runs={runs} />
    </div>
  </div>;
}

export default function App() {
  const [activePage, setActivePage] = useState("Dashboard");
  const [search, setSearch] = useState("");
  const [health, setHealth] = useState({ online: false, status: "checking" });
  const [settings, setSettings] = useState(null);
  const [systemRuntimeInfo, setSystemRuntimeInfo] = useState(null);
  const [loading, setLoading] = useState("");
  const [error, setError] = useState("");
  const [lastResponse, setLastResponse] = useState(null);
  const [notice, setNotice] = useState("");
  const [latestScanRunId, setLatestScanRunId] = useState("");
  const [scanRows, setScanRows] = useState([]);
  const [swingRows, setSwingRows] = useState([]);
  const [momentumRows, setMomentumRows] = useState([]);
  const [summary, setSummary] = useState(null);
  const [scoreSummary, setScoreSummary] = useState(null);
  const [swingSummary, setSwingSummary] = useState(null);
  const [momentumSummary, setMomentumSummary] = useState(null);
  const [dashboardEquity, setDashboardEquity] = useState(null);
  const [aiDatasetSummary, setAiDatasetSummary] = useState(null);
  const [aiFeatureSnapshots, setAiFeatureSnapshots] = useState([]);
  const [aiOutcomePreview, setAiOutcomePreview] = useState(null);
  const [aiDataCollectionStatus, setAiDataCollectionStatus] = useState(null);
  const [aiDatasetFilters, setAiDatasetFilters] = useState({ strategyType: "", timeframe: "" });
  const [aiDatasetErrors, setAiDatasetErrors] = useState([]);
  const [dashboardErrors, setDashboardErrors] = useState([]);
  const [paperUpdateProgress, setPaperUpdateProgress] = useState(null);
  const [paperUpdateRuns, setPaperUpdateRuns] = useState([]);
  const [paperUpdateLock, setPaperUpdateLock] = useState(null);
  const [paperUpdateScheduler, setPaperUpdateScheduler] = useState(null);
  const [paperOpenTrades, setPaperOpenTrades] = useState({ waiting_for_entry: [], active_partial: [] });
  const [paperHistory, setPaperHistory] = useState({ completed: [], sl_hit: [], ambiguous: [] });
  const [paperLiveStatus, setPaperLiveStatus] = useState({ running: false, lastUpdated: "", error: "", hasLoaded: false });
  const actionCycleRef = useRef(false);
  const dashboardRefreshCycleRef = useRef(false);
  const dashboardRefreshAbortRef = useRef(null);
  const globalStatusCycleRef = useRef(false);
  const globalStatusAbortRef = useRef(null);
  const paperLiveCycleRef = useRef(false);
  const paperLiveAbortRef = useRef(null);
  const paperSafetyCycleRef = useRef(false);
  const paperSafetyAbortRef = useRef(null);
  const tvRuntimeCycleRef = useRef(false); // tvRuntimeCycleRef.current
  const tvActionInProgressRef = useRef(false);
  const tvPollRequestRef = useRef(0);
  const tvPollAbortRef = useRef(null);
  const tvActionRequestRef = useRef(0);
  const tvActionAbortRef = useRef(null);
  const tvTabsRequestRef = useRef(0);
  const tvTabsAbortRef = useRef(null);
  const tvPollTimeoutRef = useRef(null);
  const stockDetailRequestRef = useRef(0);
  const stockDetailAbortRef = useRef(null);
  const swingCandidatesAbortRef = useRef(null);
  const momentumCandidatesAbortRef = useRef(null);
  const [tv, setTv] = useState({ symbol: "NSE:RELIANCE", timeframe: "1D" });
  const [tvResult, setTvResult] = useState(null);
  const [tvRuntimeStatus, setTvRuntimeStatus] = useState(null);
  const [tvRuntimeLastUpdatedAt, setTvRuntimeLastUpdatedAt] = useState(null);
  const [tvRuntimeLoading, setTvRuntimeLoading] = useState(false);
  const [tvRuntimeRefreshError, setTvRuntimeRefreshError] = useState(null);
  const [tvAttachableTabs, setTvAttachableTabs] = useState(null);
  const [marketLoadResult, setMarketLoadResult] = useState(null);
  const [marketProgress, setMarketProgress] = useState(null);
  const [scoreRunResult, setScoreRunResult] = useState(null);
  const [marketDataNeedsScore, setMarketDataNeedsScore] = useState(false);
  const [swingCandidatesStale, setSwingCandidatesStale] = useState(false);
  const [momentumCandidatesStale, setMomentumCandidatesStale] = useState(false);
  const [swingTvResult, setSwingTvResult] = useState(null);
  const [swingSavedTvResult, setSwingSavedTvResult] = useState(null);
  const [latestSwingTvRows, setLatestSwingTvRows] = useState([]);
  const [swingTvRowsLoaded, setSwingTvRowsLoaded] = useState(false);
  const [swingTvLimit, setSwingTvLimit] = useState(1);
  const [swingTvSave, setSwingTvSave] = useState(false);
  const [swingBatchResults, setSwingBatchResults] = useState([]);
  const [swingBatchProgress, setSwingBatchProgress] = useState(null);
  const [swingBatchError, setSwingBatchError] = useState(null);
  const [swingBatchStopRequested, setSwingBatchStopRequested] = useState(false);
  const [swingBatchStopMessage, setSwingBatchStopMessage] = useState("");
  const swingBatchStopRef = useRef(false);
  const [momentumTvResult, setMomentumTvResult] = useState(null);
  const [momentumSavedTvResult, setMomentumSavedTvResult] = useState(null);
  const [latestMomentumTvRows, setLatestMomentumTvRows] = useState([]);
  const [momentumTvRowsLoaded, setMomentumTvRowsLoaded] = useState(false);
  const [momentumTvLimit, setMomentumTvLimit] = useState(1);
  const [momentumTvTimeframes, setMomentumTvTimeframes] = useState(MOMENTUM_MTF_TIMEFRAMES);
  const [momentumTvElapsed, setMomentumTvElapsed] = useState(0);
  const [momentumBatchTotal, setMomentumBatchTotal] = useState(10);
  const [momentumBatchSize, setMomentumBatchSize] = useState(10);
  const [momentumBatchSave, setMomentumBatchSave] = useState(true);
  const [momentumBatchResults, setMomentumBatchResults] = useState([]);
  const [momentumBatchProgress, setMomentumBatchProgress] = useState(null);
  const [momentumBatchError, setMomentumBatchError] = useState(null);
  const [momentumBatchStopRequested, setMomentumBatchStopRequested] = useState(false);
  const [momentumBatchStopMessage, setMomentumBatchStopMessage] = useState("");
  const momentumBatchStopRef = useRef(false);
  const [stockMarketData, setStockMarketData] = useState(null);
  const [stockSwingPrecheck, setStockSwingPrecheck] = useState(null);
  const [stockMomentumPrecheck, setStockMomentumPrecheck] = useState(null);
  const [stockSwingTvResult, setStockSwingTvResult] = useState(null);
  const [stockMomentumTvResult, setStockMomentumTvResult] = useState(null);
  const [stockSavedSwingResult, setStockSavedSwingResult] = useState(null);
  const [stockSavedMomentumResult, setStockSavedMomentumResult] = useState(null);
  const [stockSwingTimeframes, setStockSwingTimeframes] = useState(SWING_MTF_TIMEFRAMES);
  const [stockMomentumTimeframes, setStockMomentumTimeframes] = useState(MOMENTUM_MTF_TIMEFRAMES);

  const fetchAndSetTvRuntimeStatusPoll = async (options = {}) => {
    if (tvActionInProgressRef.current) return null;
    const requestId = tvPollRequestRef.current + 1;
    tvPollRequestRef.current = requestId;
    const controller = new AbortController();
    if (!options.signal) {
      tvPollAbortRef.current = controller;
    }
    const signal = options.signal || controller.signal;
    try {
      const status = await getTradingViewRuntimeStatus({ ...options, signal });
      if (requestId === tvPollRequestRef.current && !tvActionInProgressRef.current) {
        setTvRuntimeStatus(status);
        setTvRuntimeLastUpdatedAt(Date.now());
        setTvRuntimeRefreshError(null);
      }
      return status;
    } catch (err) {
      const cancelled = isRequestCancellation(err) || signal.aborted;
      if (requestId === tvPollRequestRef.current && !tvActionInProgressRef.current) {
        if (!cancelled) {
          setTvRuntimeRefreshError(err.message || String(err));
        }
      }
      throw err;
    } finally {
      if (tvPollAbortRef.current === controller) {
        tvPollAbortRef.current = null;
      }
    }
  };

  const fetchAndSetTvRuntimeStatusAction = async (options = {}) => {
    const requestId = tvActionRequestRef.current + 1;
    tvActionRequestRef.current = requestId;
    if (options.manual === true || options.forceAbort === true) {
      tvActionAbortRef.current?.abort();
    }
    const controller = new AbortController();
    if (!options.signal) {
      tvActionAbortRef.current = controller;
    }
    const signal = options.signal || controller.signal;
    if (requestId === tvActionRequestRef.current) {
      setTvRuntimeLoading(true);
    }
    try {
      const status = await getTradingViewRuntimeStatus({ ...options, signal });
      if (requestId === tvActionRequestRef.current) {
        setTvRuntimeStatus(status);
        setTvRuntimeLastUpdatedAt(Date.now());
        setTvRuntimeRefreshError(null);
        setTvRuntimeLoading(false);
      }
      return status;
    } catch (err) {
      const cancelled = isRequestCancellation(err) || signal.aborted;
      if (requestId === tvActionRequestRef.current) {
        setTvRuntimeLoading(false);
        if (!cancelled) {
          setTvRuntimeRefreshError(err.message || String(err));
        }
      }
      throw err;
    } finally {
      if (tvActionAbortRef.current === controller) {
        tvActionAbortRef.current = null;
      }
    }
  };

  const fetchAndSetTvAttachableTabs = async (options = {}) => {
    const requestId = tvTabsRequestRef.current + 1;
    tvTabsRequestRef.current = requestId;
    if (options.manual === true || options.forceAbort === true) {
      tvTabsAbortRef.current?.abort();
    }
    const controller = new AbortController();
    if (!options.signal) {
      tvTabsAbortRef.current = controller;
    }
    const signal = options.signal || controller.signal;
    try {
      const tabs = await getTradingViewAttachableTabs({ ...options, signal });
      if (requestId === tvTabsRequestRef.current) {
        setTvAttachableTabs(tabs);
      }
      return tabs;
    } catch (err) {
      throw err;
    } finally {
      if (tvTabsAbortRef.current === controller) {
        tvTabsAbortRef.current = null;
      }
    }
  };

  const act = async (name, fn) => {
    if (actionCycleRef.current) return null;
    actionCycleRef.current = true;
    setLoading(name); setError(""); setNotice("");
    try {
      const data = await fn();
      setLastResponse(data);
      setError("");
      return data;
    } catch (err) {
      if (isRequestCancellation(err)) return null;
      console.error(`${name} failed`, err);
      setError(formatActionError(err, name));
      return null;
    } finally {
      actionCycleRef.current = false;
      setLoading("");
    }
  };

  const refreshGlobalHealth = async ({ includeSettings = false, includeRuntime = false, isCancelled = () => false } = {}) => {
    if (globalStatusCycleRef.current) return null;
    const controller = new AbortController();
    globalStatusCycleRef.current = true;
    globalStatusAbortRef.current = controller;
    const requestOptions = { signal: controller.signal };
    try {
      const [healthData, settingsData, systemRuntime, tradingViewStatus] = await Promise.all([
        getHealth(requestOptions),
        includeSettings ? getSettings(requestOptions) : Promise.resolve(null),
        includeSettings ? getSystemRuntimeInfo(requestOptions) : Promise.resolve(null),
        includeRuntime ? fetchAndSetTvRuntimeStatusAction(requestOptions) : Promise.resolve(null),
      ]);
      if (!isCancelled()) {
        setHealth({ online: healthData?.status === "ok", status: healthData?.status || "unknown" });
        if (settingsData) setSettings(settingsData);
        if (systemRuntime) setSystemRuntimeInfo(systemRuntime);
      }
      return { health: healthData, settings: settingsData, system_runtime: systemRuntime, tradingview_runtime: tradingViewStatus };
    } catch (err) {
      if (!isCancelled() && !isRequestCancellation(err)) {
        console.error("global health poll failed", err);
        setHealth({ online: false, status: "offline" });
      }
      return null;
    } finally {
      if (globalStatusAbortRef.current === controller) {
        globalStatusAbortRef.current = null;
        globalStatusCycleRef.current = false;
      }
    }
  };

  const refreshDashboardSnapshot = async (isCancelled = () => false) => {
    if (dashboardRefreshCycleRef.current) return null;
    const controller = new AbortController();
    dashboardRefreshCycleRef.current = true;
    dashboardRefreshAbortRef.current = controller;
    const requestOptions = { signal: controller.signal };
    try {
      const results = await Promise.allSettled([
        getDashboardPaperEquity(requestOptions),
        getPaperSummary(requestOptions),
        getScoreSummary("BROAD_MARKET_750", requestOptions),
        getSwingSummary("BROAD_MARKET_750", requestOptions),
        getMomentumSummary("BROAD_MARKET_750", requestOptions),
        getPaperUpdateProgress(requestOptions),
        getPaperUpdateRuns(10, requestOptions),
        getPaperUpdateLock(requestOptions),
        getPaperUpdateSchedulerStatus(requestOptions),
        fetchAndSetTvRuntimeStatusAction(requestOptions),
      ]);
      if (!isCancelled()) {
        const [equity, paperSummary, scoreData, swingData, momentumData, progress, runs, lock, scheduler, tradingViewStatus] = results;
        if (equity.status === "fulfilled") setDashboardEquity(equity.value);
        if (paperSummary.status === "fulfilled") setSummary(paperSummary.value);
        if (scoreData.status === "fulfilled") setScoreSummary(scoreData.value);
        if (swingData.status === "fulfilled") setSwingSummary(swingData.value);
        if (momentumData.status === "fulfilled") setMomentumSummary(momentumData.value);
        if (progress.status === "fulfilled") setPaperUpdateProgress(progress.value);
        if (runs.status === "fulfilled") setPaperUpdateRuns(arr(runs.value, ["runs"]));
        if (lock.status === "fulfilled") setPaperUpdateLock(lock.value);
        if (scheduler.status === "fulfilled") setPaperUpdateScheduler(scheduler.value);

        const errors = settledErrors([
          { endpoint: "/api/dashboard/paper-equity", result: equity },
          { endpoint: "/api/paper/summary", result: paperSummary },
          { endpoint: "/api/score/summary", result: scoreData },
          { endpoint: "/api/swing/summary", result: swingData },
          { endpoint: "/api/momentum/summary", result: momentumData },
          { endpoint: "/api/paper/update-progress", result: progress },
          { endpoint: "/api/paper/update-runs", result: runs },
          { endpoint: "/api/paper/update-lock", result: lock },
          { endpoint: "/api/paper/update-scheduler/status", result: scheduler },
          { endpoint: "/api/tv/runtime-status", result: tradingViewStatus },
        ]);
        setDashboardErrors(errors);
      }
      return {
        equity: settledValue(results[0]),
        paperSummary: settledValue(results[1]),
        scoreData: settledValue(results[2]),
        swingData: settledValue(results[3]),
        momentumData: settledValue(results[4]),
        progress: settledValue(results[5]),
        runs: settledValue(results[6]),
        lock: settledValue(results[7]),
        scheduler: settledValue(results[8]),
        tradingViewStatus: settledValue(results[9]),
      };
    } catch (err) {
      if (!isCancelled() && !isRequestCancellation(err)) console.error("dashboard snapshot refresh failed", err);
      return null;
    } finally {
      if (dashboardRefreshAbortRef.current === controller) {
        dashboardRefreshAbortRef.current = null;
        dashboardRefreshCycleRef.current = false;
      }
    }
  };

  const refreshPaperUpdateSafety = async () => {
    if (paperSafetyCycleRef.current) return null;
    const controller = new AbortController();
    paperSafetyCycleRef.current = true;
    paperSafetyAbortRef.current = controller;
    const requestOptions = { signal: controller.signal };
    try {
      const [progress, runs, lock, scheduler] = await Promise.all([
        getPaperUpdateProgress(requestOptions),
        getPaperUpdateRuns(10, requestOptions),
        getPaperUpdateLock(requestOptions),
        getPaperUpdateSchedulerStatus(requestOptions),
      ]);
      const runRows = arr(runs, ["runs"]);
      setPaperUpdateProgress(progress);
      setPaperUpdateRuns(runRows);
      setPaperUpdateLock(lock);
      setPaperUpdateScheduler(scheduler);
      return { progress, runs: runRows, lock, scheduler };
    } finally {
      if (paperSafetyAbortRef.current === controller) {
        paperSafetyAbortRef.current = null;
        paperSafetyCycleRef.current = false;
      }
    }
  };
  const runPaperLiveCycle = async (isCancelled = () => false) => {
    if (paperLiveCycleRef.current) return null;
    const controller = new AbortController();
    paperLiveCycleRef.current = true;
    paperLiveAbortRef.current = controller;
    if (!isCancelled()) setPaperLiveStatus((current) => ({ ...current, running: true }));
    try {
      const requestOptions = { signal: controller.signal };
      const results = await Promise.allSettled([
        getPaperOpenTrades(requestOptions),
        getPaperHistory(requestOptions),
        getPaperSummary(requestOptions),
      ]);
      const errors = [];
      let successCount = 0;
      const applyResult = (index, label, apply) => {
        const result = results[index];
        if (result.status === "fulfilled") {
          successCount += 1;
          if (!isCancelled()) apply(result.value);
          return;
        }
        if (isRequestCancellation(result.reason)) return;
        if (!isCancelled()) {
          console.error(`${label} failed`, result.reason);
          errors.push(`${label}: ${result.reason?.message || String(result.reason)}`);
        }
      };
      applyResult(0, "paper open trades refresh", setPaperOpenTrades);
      applyResult(1, "paper history refresh", setPaperHistory);
      applyResult(2, "paper summary refresh", setSummary);
      if (!isCancelled()) {
        setPaperLiveStatus((current) => ({
          running: false,
          lastUpdated: new Date().toLocaleTimeString(),
          error: errors.length ? errors.join(" | ") : "",
          hasLoaded: current.hasLoaded || successCount > 0,
        }));
      }
      return { errors };
    } finally {
      if (paperLiveAbortRef.current === controller) {
        paperLiveAbortRef.current = null;
        paperLiveCycleRef.current = false;
      }
      if (!isCancelled()) setPaperLiveStatus((current) => ({ ...current, running: false }));
    }
  };

  useEffect(() => {
    if (loading !== "momentum tv confirm") {
      setMomentumTvElapsed(0);
      return undefined;
    }
    const startedAt = Date.now();
    setMomentumTvElapsed(0);
    const intervalId = window.setInterval(() => {
      setMomentumTvElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(intervalId);
  }, [loading]);

  useEffect(() => {
    if (loading !== "swing batch tv confirm" && loading !== "momentum batch tv confirm") return undefined;
    const intervalId = window.setInterval(() => {
      const now = Date.now();
      setSwingBatchProgress((current) => current?.started_at ? { ...current, elapsed_time: Math.floor((now - current.started_at) / 1000) } : current);
      setMomentumBatchProgress((current) => current?.started_at ? { ...current, elapsed_time: Math.floor((now - current.started_at) / 1000) } : current);
    }, 1000);
    return () => window.clearInterval(intervalId);
  }, [loading]);

  useEffect(() => {
    const isTradingPage = activePage === "Swing Trading" || activePage === "Momentum Trading";
    if (!isTradingPage) {
      return undefined;
    }
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      if (tvActionInProgressRef.current) {
        if (!cancelled) {
          tvPollTimeoutRef.current = window.setTimeout(poll, BATCH_TV_RUNTIME_REFRESH_MS);
        }
        return;
      }
      try {
        await fetchAndSetTvRuntimeStatusPoll();
      } catch (err) {
        if (!cancelled && !isRequestCancellation(err)) {
          console.error("TV runtime polling failed", err);
        }
      } finally {
        if (!cancelled) {
          tvPollTimeoutRef.current = window.setTimeout(poll, BATCH_TV_RUNTIME_REFRESH_MS);
        }
      }
    };
    poll();
    return () => {
      cancelled = true;
      if (tvPollTimeoutRef.current) {
        window.clearTimeout(tvPollTimeoutRef.current);
        tvPollTimeoutRef.current = null;
      }
      tvPollAbortRef.current?.abort();
    };
  }, [activePage]);

  useEffect(() => {
    let cancelled = false;
    const isCancelled = () => cancelled;
    refreshGlobalHealth({ includeSettings: true, includeRuntime: true, isCancelled });
    const intervalId = window.setInterval(() => {
      refreshGlobalHealth({ isCancelled });
    }, GLOBAL_HEALTH_REFRESH_MS);
    return () => {
      cancelled = true;
      const activeController = globalStatusAbortRef.current;
      activeController?.abort();
      if (activeController) {
        globalStatusAbortRef.current = null;
        globalStatusCycleRef.current = false;
      }
      window.clearInterval(intervalId);
    };
  }, []);

  useEffect(() => {
    if (activePage !== "Settings" && activePage !== "Swing Trading" && activePage !== "Momentum Trading") return undefined;
    let cancelled = false;
    const controller = new AbortController();
    refreshGlobalHealth({ includeSettings: true, includeRuntime: false, isCancelled: () => cancelled });
    fetchAndSetTvRuntimeStatusAction({ signal: controller.signal })
      .catch((err) => {
        if (!cancelled && !isRequestCancellation(err)) console.error("TradingView runtime status refresh failed", err);
      });
    return () => {
      cancelled = true;
      controller.abort();
      tvTabsAbortRef.current?.abort();
      tvActionAbortRef.current?.abort();
    };
  }, [activePage]);

  useEffect(() => () => {
    dashboardRefreshAbortRef.current?.abort();
    globalStatusAbortRef.current?.abort();
    paperLiveAbortRef.current?.abort();
    paperSafetyAbortRef.current?.abort();
    tvActionAbortRef.current?.abort();
    tvPollAbortRef.current?.abort();
    tvTabsAbortRef.current?.abort();
    stockDetailAbortRef.current?.abort();
    swingCandidatesAbortRef.current?.abort();
    momentumCandidatesAbortRef.current?.abort();
    if (tvPollTimeoutRef.current) {
      window.clearTimeout(tvPollTimeoutRef.current);
      tvPollTimeoutRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (activePage !== "Dashboard") return undefined;
    let cancelled = false;
    const isCancelled = () => cancelled;
    refreshDashboardSnapshot(isCancelled);
    const intervalId = window.setInterval(() => {
      refreshDashboardSnapshot(isCancelled);
    }, DASHBOARD_REFRESH_MS);
    return () => {
      cancelled = true;
      const activeController = dashboardRefreshAbortRef.current;
      activeController?.abort();
      if (activeController) {
        dashboardRefreshAbortRef.current = null;
        dashboardRefreshCycleRef.current = false;
      }
      window.clearInterval(intervalId);
    };
  }, [activePage]);

  useEffect(() => {
    if (activePage !== "Dashboard") return undefined;
    let cancelled = false;
    const controller = new AbortController();
    const requestOptions = { signal: controller.signal };
    const filterStrategy = aiDatasetFilters.strategyType || "";
    const filterTf = aiDatasetFilters.timeframe ? normalizeTimeframe(aiDatasetFilters.timeframe) : "";
    const cleanTf = (filterTf && SUPPORTED_TIMEFRAMES.has(filterTf)) ? filterTf : "";
    const queryParams = { strategyType: filterStrategy, timeframe: cleanTf };
    Promise.allSettled([
      getAiFeatureDatasetSummary(queryParams, requestOptions),
      getAiFeatureSnapshots(50, requestOptions),
      getAiOutcomePreview(50, requestOptions),
      getAiDataCollectionStatus(requestOptions),
    ])
      .then((results) => {
        if (!cancelled) {
          const [summaryData, snapshotsData, outcomePreviewData, collectionStatusData] = results;
          if (summaryData.status === "fulfilled") setAiDatasetSummary(summaryData.value);
          if (snapshotsData.status === "fulfilled") setAiFeatureSnapshots(arr(snapshotsData.value, ["rows"]));
          if (outcomePreviewData.status === "fulfilled") setAiOutcomePreview(outcomePreviewData.value);
          if (collectionStatusData.status === "fulfilled") setAiDataCollectionStatus(collectionStatusData.value);
          setAiDatasetErrors(settledErrors([
            { endpoint: "/api/ai/features/summary", result: summaryData },
            { endpoint: "/api/ai/features/snapshots", result: snapshotsData },
            { endpoint: "/api/ai/features/outcome-preview", result: outcomePreviewData },
            { endpoint: "/api/ai/features/collection-status", result: collectionStatusData },
          ]));
        }
      })
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [activePage, aiDatasetFilters]);

  useEffect(() => {
    if (activePage !== "Paper Trades") return undefined;
    let cancelled = false;
    const isCancelled = () => cancelled;
    runPaperLiveCycle(isCancelled);
    const intervalId = window.setInterval(() => {
      runPaperLiveCycle(isCancelled);
    }, PAPER_TRADES_REFRESH_MS);
    return () => {
      cancelled = true;
      const activeController = paperLiveAbortRef.current;
      activeController?.abort();
      if (activeController) {
        paperLiveAbortRef.current = null;
        paperLiveCycleRef.current = false;
      }
      window.clearInterval(intervalId);
    };
  }, [activePage]);

  useEffect(() => {
    const searched = normalizeSearchSymbol(search);
    stockDetailAbortRef.current?.abort();
    const requestId = stockDetailRequestRef.current + 1;
    stockDetailRequestRef.current = requestId;
    const controller = new AbortController();
    stockDetailAbortRef.current = controller;
    setStockSwingPrecheck(null);
    setStockMomentumPrecheck(null);
    setStockSwingTvResult(null);
    setStockMomentumTvResult(null);
    setStockSavedSwingResult(null);
    setStockSavedMomentumResult(null);
    if (activePage !== "Stock Detail" || !searched.symbol) {
      setStockMarketData(null);
      return () => {
        controller.abort();
        if (stockDetailAbortRef.current === controller) stockDetailAbortRef.current = null;
      };
    }
    const timerId = window.setTimeout(() => {
      const requestOptions = { signal: controller.signal };
      Promise.all([
        getMarketDataSymbol({ exchange: searched.exchange, symbol: searched.symbol }, requestOptions),
        getSwingPrecheck({ exchange: searched.exchange, symbol: searched.symbol }, requestOptions),
        getMomentumPrecheck({ exchange: searched.exchange, symbol: searched.symbol }, requestOptions),
        getSwingTvConfirmed({ limit: 200 }, requestOptions),
        getMomentumTvConfirmed({ limit: 200 }, requestOptions),
      ])
        .then(([marketData, swingPrecheck, momentumPrecheck, savedSwing, savedMomentum]) => {
          if (controller.signal.aborted || stockDetailRequestRef.current !== requestId) return;
          setStockMarketData(marketData);
          setStockSwingPrecheck(swingPrecheck);
          setStockMomentumPrecheck(momentumPrecheck);
          setStockSavedSwingResult(savedSwing);
          setStockSavedMomentumResult(savedMomentum);
        })
        .catch((err) => {
          if (controller.signal.aborted || stockDetailRequestRef.current !== requestId || isRequestCancellation(err)) return;
          console.error("stock detail load failed", err);
          setStockMarketData({ found: false, error: err.message || String(err) });
        });
    }, 350);
    return () => {
      window.clearTimeout(timerId);
      controller.abort();
      if (stockDetailAbortRef.current === controller) stockDetailAbortRef.current = null;
    };
  }, [activePage, search]);

  const openStockDetail = (row) => {
    const nextSearch = rowSearchSymbol(row);
    if (!nextSearch) return;
    setSearch(nextSearch);
    setActivePage("Stock Detail");
  };

  const getPaperSafeScanRows = async (scanRunId) => {
    try { return arr(await getScanRows(scanRunId), ["rows"]); } catch { return []; }
  };
  const handlers = {
    loadSummary: () => act("summary", async () => { const data = await getPaperSummary(); setSummary(data); return data; }),
    aiDatasetSummary: () => act("AI dataset summary", async () => {
      const filterStrategy = aiDatasetFilters.strategyType || "";
      const filterTf = aiDatasetFilters.timeframe ? normalizeTimeframe(aiDatasetFilters.timeframe) : "";
      const cleanTf = (filterTf && SUPPORTED_TIMEFRAMES.has(filterTf)) ? filterTf : "";
      const queryParams = { strategyType: filterStrategy, timeframe: cleanTf };
      const results = await Promise.allSettled([
        getAiFeatureDatasetSummary(queryParams),
        getAiFeatureSnapshots(50),
        getAiOutcomePreview(50),
        getAiDataCollectionStatus(),
      ]);
      const [summaryResult, snapshotsResult, outcomePreviewResult, collectionStatusResult] = results;
      const summaryData = settledValue(summaryResult, aiDatasetSummary);
      const snapshotsData = settledValue(snapshotsResult, { rows: aiFeatureSnapshots });
      const outcomePreviewData = settledValue(outcomePreviewResult, aiOutcomePreview);
      const collectionStatusData = settledValue(collectionStatusResult, aiDataCollectionStatus);
      setAiDatasetSummary(summaryData);
      setAiFeatureSnapshots(arr(snapshotsData, ["rows"]));
      setAiOutcomePreview(outcomePreviewData);
      setAiDataCollectionStatus(collectionStatusData);
      const errors = settledErrors([
        { endpoint: "/api/ai/features/summary", result: summaryResult },
        { endpoint: "/api/ai/features/snapshots", result: snapshotsResult },
        { endpoint: "/api/ai/features/outcome-preview", result: outcomePreviewResult },
        { endpoint: "/api/ai/features/collection-status", result: collectionStatusResult },
      ]);
      setAiDatasetErrors(errors);
      return { summary: summaryData, snapshots: snapshotsData, outcome_preview: outcomePreviewData, collection_status: collectionStatusData, partial_errors: errors };
    }),
    dryRun: () => act("pipeline dry run", () => runPaperPipeline({ limit: 1, timeframe: "1D", dryRun: true, strategy: "swing" })),
    saveRun: () => act("pipeline save", () => runPaperPipeline({ limit: 1, timeframe: "1D", dryRun: false, strategy: "swing" })),
    tvRefreshStatus: () => act("refresh tv status", async () => {
      tvActionInProgressRef.current = true;
      if (tvPollTimeoutRef.current) {
        window.clearTimeout(tvPollTimeoutRef.current);
        tvPollTimeoutRef.current = null;
      }
      tvPollAbortRef.current?.abort();
      try {
        const status = await fetchAndSetTvRuntimeStatusAction({ manual: true });
        return status;
      } finally {
        tvActionInProgressRef.current = false;
      }
    }),
    tvRefreshTabs: () => act("tv tabs", async () => {
      tvActionInProgressRef.current = true;
      if (tvPollTimeoutRef.current) {
        window.clearTimeout(tvPollTimeoutRef.current);
        tvPollTimeoutRef.current = null;
      }
      tvPollAbortRef.current?.abort();
      try {
        const tabs = await fetchAndSetTvAttachableTabs({ manual: true });
        const status = await fetchAndSetTvRuntimeStatusAction({ manual: true });
        return { attachable_tabs: tabs, runtime: status };
      } finally {
        tvActionInProgressRef.current = false;
      }
    }),
    tvAttachTab: (targetId) => act("tv attach tab", async () => {
      if (!targetId) throw new Error("Missing TradingView target id.");
      tvActionInProgressRef.current = true;
      if (tvPollTimeoutRef.current) {
        window.clearTimeout(tvPollTimeoutRef.current);
        tvPollTimeoutRef.current = null;
      }
      tvPollAbortRef.current?.abort();
      try {
        const attached = await attachTradingViewTab(targetId);
        const tabs = await fetchAndSetTvAttachableTabs({ manual: true });
        const status = await fetchAndSetTvRuntimeStatusAction({ manual: true });
        return { attached, attachable_tabs: tabs, runtime: status };
      } finally {
        tvActionInProgressRef.current = false;
      }
    }),
    tvDetachTab: () => act("tv detach tab", async () => {
      tvActionInProgressRef.current = true;
      if (tvPollTimeoutRef.current) {
        window.clearTimeout(tvPollTimeoutRef.current);
        tvPollTimeoutRef.current = null;
      }
      tvPollAbortRef.current?.abort();
      try {
        const detached = await detachTradingViewTab();
        const tabs = await fetchAndSetTvAttachableTabs({ manual: true });
        const status = await fetchAndSetTvRuntimeStatusAction({ manual: true });
        return { detached, attachable_tabs: tabs, runtime: status };
      } finally {
        tvActionInProgressRef.current = false;
      }
    }),
    scoreMarketData: () => act("score market data", async () => {
      const data = await runScoring();
      const frontendScoredAt = new Date().toLocaleString();
      setScoreRunResult({ ...data, frontend_scored_at: frontendScoredAt });
      setSwingRows([]);
      setMomentumRows([]);
      setSwingTvResult(null);
      setMomentumTvResult(null);
      setSwingBatchResults([]);
      setMomentumBatchResults([]);
      setLatestSwingTvRows([]);
      setLatestMomentumTvRows([]);
      setSwingTvRowsLoaded(false);
      setMomentumTvRowsLoaded(false);
      setStockSwingTvResult(null);
      setStockMomentumTvResult(null);
      const [scoreData, swingData, momentumData, progress] = await Promise.all([getScoreSummary(), getSwingSummary(), getMomentumSummary(), getMarketLoadProgress()]);
      setScoreSummary(scoreData); setSwingSummary(swingData); setMomentumSummary(momentumData);
      setSwingTvLimit(positiveCount(swingData?.swing_candidates_count));
      setMomentumTvLimit(positiveCount(momentumData?.momentum_candidates_count));
      setMomentumBatchTotal(positiveCount(momentumData?.momentum_candidates_count || 10));
      setMarketProgress(progress);
      setSwingCandidatesStale(Boolean(swingData?.is_score_stale));
      setMomentumCandidatesStale(Boolean(momentumData?.is_score_stale));
      setMarketDataNeedsScore(Boolean(scoreData?.is_score_stale));
      setNotice(SCORE_REFRESH_MESSAGE);
      return { score_run: data, score_summary: scoreData, swing_summary: swingData, momentum_summary: momentumData };
    }),
    swingSummary: () => act("swing summary", async () => { const data = await getSwingSummary(); setSwingSummary(data); setSwingTvLimit(positiveCount(data?.swing_candidates_count)); setSwingCandidatesStale(Boolean(data?.is_score_stale)); return data; }),
    swing: () => act("swing candidates", async () => {
      swingCandidatesAbortRef.current?.abort();
      const controller = new AbortController();
      swingCandidatesAbortRef.current = controller;
      setSwingRows([]);
      try {
        const options = { signal: controller.signal };
        const summaryData = await getSwingSummary("BROAD_MARKET_750", options);
        const limit = positiveCount(summaryData?.swing_candidates_count);
        const data = await getSwingCandidates("BROAD_MARKET_750", limit, options);
        setSwingRows(arr(data, ["candidates", "rows"]));
        setSwingSummary(summaryData);
        setSwingTvLimit(limit);
        setSwingCandidatesStale(Boolean(summaryData?.is_score_stale));
        return data;
      } catch (err) {
        setSwingRows([]);
        throw err;
      }
    }),
    swingTvConfirm: () => act("swing batch tv confirm", async () => {
      tvActionInProgressRef.current = true;
      if (tvPollTimeoutRef.current) {
        window.clearTimeout(tvPollTimeoutRef.current);
        tvPollTimeoutRef.current = null;
      }
      tvPollAbortRef.current?.abort();
      try {
        const status = await fetchAndSetTvRuntimeStatusAction({ manual: true, forceAbort: true });
        if (!status || !canStartTradingViewOperation(status)) {
          throw new Error(status?.preflight_message || "TradingView is not ready for batch confirm.");
        }
        if (deriveTradingViewBusy(status)) {
          throw new Error("TradingView is busy with another operation.");
        }

        const summaryData = await getSwingSummary();
        const summaryCount = countValue(summaryData?.swing_candidates_count);
        let candidateRows = Array.isArray(swingRows) ? swingRows : [];
        const totalTarget = summaryCount || candidateRows.length;
        if (totalTarget > 0 && candidateRows.length < totalTarget) {
          const candidateData = await getSwingCandidates("BROAD_MARKET_750", totalTarget);
          candidateRows = arr(candidateData, ["candidates", "rows"]);
          setSwingRows(candidateRows);
        }
        const totalRequested = Math.min(totalTarget || candidateRows.length, candidateRows.length || totalTarget);
        if (totalRequested <= 0) throw new Error("No Swing candidates available for TV Confirm.");
        const batchSize = SWING_TV_BATCH_SIZE;
        const totalBatches = Math.ceil(totalRequested / batchSize);
        const startedAt = Date.now();
        let processedSoFar = 0;
        const completed = [];
        swingBatchStopRef.current = false;
        setSwingBatchStopRequested(false);
        setSwingBatchStopMessage("");
        setSwingBatchError(null);
        setSwingBatchResults([]);
        setLatestSwingTvRows([]);
        setSwingTvRowsLoaded(true);
        setSwingTvResult(null);
        setSwingSummary(summaryData);
        for (let offset = 0; offset < totalRequested; offset += batchSize) {
          const batchNumber = Math.floor(offset / batchSize) + 1;
          const currentLimit = Math.min(batchSize, totalRequested - offset);
          const rangeEnd = offset + currentLimit;
          const symbols = candidateSymbols(candidateRows, offset, currentLimit);
          setSwingBatchProgress({
            process_type: "Swing TV Confirm",
            current_batch_number: batchNumber,
            total_batches: totalBatches,
            batch_size: batchSize,
            total_requested: totalRequested,
            processed_so_far: processedSoFar,
            current_batch_range: `${offset + 1}-${rangeEnd}`,
            current_batch_symbols: symbols,
            started_at: startedAt,
            elapsed_time: Math.floor((Date.now() - startedAt) / 1000),
          });
          try {
            let batch = null;
            for (let retryAttempt = 0; retryAttempt <= TV_BUSY_MAX_RETRIES; retryAttempt++) {
              try {
                batch = await swingTvConfirm({ limit: currentLimit, offset, batchNumber, batchSize, timeframes: SWING_MTF_TIMEFRAMES, save: true });
                break;
              } catch (retryErr) {
                if (isTvManagerBusy(retryErr) && retryAttempt < TV_BUSY_MAX_RETRIES) {
                  console.warn(`Swing batch ${batchNumber}: TV_MANAGER_BUSY, retry ${retryAttempt + 1}/${TV_BUSY_MAX_RETRIES} after ${TV_BUSY_BASE_DELAY_MS * (retryAttempt + 1)}ms`);
                  await delay(TV_BUSY_BASE_DELAY_MS * (retryAttempt + 1));
                  continue;
                }
                throw retryErr;
              }
            }
            const rows = tvRows(batch);
            processedSoFar += Number(batch?.processed || 0);
            completed.push(batch);
            setSwingBatchResults([...completed]);
            setLatestSwingTvRows((current) => [...current, ...rows]);
            setSwingTvRowsLoaded(true);
            if (offset === 0) {
              try {
                await fetchAndSetTvRuntimeStatusAction({ manual: true });
              } catch (statusErr) {
                console.error("Failed to refresh status after first batch attachment", statusErr);
              }
            }
            setSwingBatchProgress((current) => current ? { ...current, processed_so_far: processedSoFar, elapsed_time: Math.floor((Date.now() - startedAt) / 1000) } : current);
          } catch (err) {
            if (isRequestCancellation(err)) throw err;
            try {
              await fetchAndSetTvRuntimeStatusAction({ manual: true });
            } catch (statusErr) {
              console.error("Failed to refresh status after batch failure", statusErr);
            }
            setSwingBatchError({ batch_number: batchNumber, message: formatActionError(err, `swing batch ${batchNumber}`) });
            setSwingBatchProgress(null);
            throw err;
          }
          if (swingBatchStopRef.current) {
            setSwingBatchStopMessage(`Stopped after Batch ${batchNumber}.`);
            break;
          }
        }
        setSwingBatchProgress(null);
        try {
          await fetchAndSetTvRuntimeStatusAction({ manual: true });
        } catch (statusErr) {
          console.error("Failed to refresh status after batch completion", statusErr);
        }
        return { total_requested: totalRequested, batch_size: batchSize, batches_completed: completed.length, results: completed };
      } finally {
        tvActionInProgressRef.current = false;
      }
    }),
    swingSavedTv: () => act("saved swing tv results", async () => {
      setSwingSavedTvResult(null);
      setLatestSwingTvRows([]);
      setSwingTvRowsLoaded(false);
      const data = await getSwingTvConfirmed();
      setSwingSavedTvResult(data);
      setLatestSwingTvRows(tvRows(data));
      setSwingTvRowsLoaded(true);
      return data;
    }),
    swingStopBatch: () => { swingBatchStopRef.current = true; setSwingBatchStopRequested(true); },
    momentumSummary: () => act("momentum summary", async () => { const data = await getMomentumSummary(); const limit = positiveCount(data?.momentum_candidates_count || 10); setMomentumSummary(data); setMomentumTvLimit(limit); setMomentumBatchTotal(limit); setMomentumCandidatesStale(Boolean(data?.is_score_stale)); return data; }),
    momentum: () => act("momentum candidates", async () => {
      momentumCandidatesAbortRef.current?.abort();
      const controller = new AbortController();
      momentumCandidatesAbortRef.current = controller;
      setMomentumRows([]);
      try {
        const options = { signal: controller.signal };
        const summaryData = await getMomentumSummary("BROAD_MARKET_750", options);
        const limit = positiveCount(summaryData?.momentum_candidates_count);
        const data = await getMomentumCandidates("BROAD_MARKET_750", limit, options);
        setMomentumRows(arr(data, ["candidates", "rows"]));
        setMomentumSummary(summaryData);
        setMomentumTvLimit(limit);
        setMomentumBatchTotal(limit);
        setMomentumCandidatesStale(Boolean(summaryData?.is_score_stale));
        return data;
      } catch (err) {
        setMomentumRows([]);
        throw err;
      }
    }),
    momentumConfirm: () => act("momentum tv confirm", async () => {
      setMomentumTvResult(null);
      const summaryData = await getMomentumSummary();
      const maxLimit = positiveCount(summaryData?.momentum_candidates_count);
      const limit = momentumSummary ? clampLimit(momentumTvLimit, maxLimit) : maxLimit;
      setMomentumSummary(summaryData);
      setMomentumTvLimit(limit);
      const data = await momentumTvConfirm({ limit, timeframes: momentumTvTimeframes, save: true, forceUseStaleScores: false });
      setMomentumTvResult(data);
      return data;
    }),
    momentumSavedTv: () => act("saved momentum tv results", async () => {
      setMomentumSavedTvResult(null);
      setLatestMomentumTvRows([]);
      setMomentumTvRowsLoaded(false);
      const data = await getMomentumTvConfirmed();
      const normalized = normalizeSavedTvResponse(data);
      setMomentumSavedTvResult(normalized);
      setLatestMomentumTvRows(tvRows(normalized));
      setMomentumTvRowsLoaded(true);
      return normalized;
    }),
    momentumBatchConfirm: () => act("momentum batch tv confirm", async () => {
      tvActionInProgressRef.current = true;
      if (tvPollTimeoutRef.current) {
        window.clearTimeout(tvPollTimeoutRef.current);
        tvPollTimeoutRef.current = null;
      }
      tvPollAbortRef.current?.abort();
      try {
        const status = await fetchAndSetTvRuntimeStatusAction({ manual: true, forceAbort: true });
        if (!status || !canStartTradingViewOperation(status)) {
          throw new Error(status?.preflight_message || "TradingView is not ready for batch confirm.");
        }
        if (deriveTradingViewBusy(status)) {
          throw new Error("TradingView is busy with another operation.");
        }

        const summaryData = await getMomentumSummary();
        const summaryCount = countValue(summaryData?.momentum_candidates_count);
        let candidateRows = Array.isArray(momentumRows) ? momentumRows : [];
        const totalTarget = summaryCount || candidateRows.length;
        if (totalTarget > 0 && candidateRows.length < totalTarget) {
          const candidateData = await getMomentumCandidates("BROAD_MARKET_750", totalTarget);
          candidateRows = arr(candidateData, ["candidates", "rows"]);
          setMomentumRows(candidateRows);
        }
        const totalRequested = Math.min(totalTarget || candidateRows.length, candidateRows.length || totalTarget);
        if (totalRequested <= 0) throw new Error("No Momentum candidates available for TV Confirm.");
        const batchSize = MOMENTUM_TV_BATCH_SIZE;
        const totalBatches = Math.ceil(totalRequested / batchSize);
        const startedAt = Date.now();
        let processedSoFar = 0;
        const completed = [];
        momentumBatchStopRef.current = false;
        setMomentumBatchStopRequested(false);
        setMomentumBatchStopMessage("");
        setMomentumBatchError(null);
        setMomentumBatchResults([]);
        setLatestMomentumTvRows([]);
        setMomentumTvRowsLoaded(true);
        setMomentumSummary(summaryData);
        for (let offset = 0; offset < totalRequested; offset += batchSize) {
          const batchNumber = Math.floor(offset / batchSize) + 1;
          const currentLimit = Math.min(batchSize, totalRequested - offset);
          const rangeEnd = offset + currentLimit;
          const symbols = candidateSymbols(candidateRows, offset, currentLimit);
          setMomentumBatchProgress({
            process_type: "Momentum TV Confirm",
            current_batch_number: batchNumber,
            total_batches: totalBatches,
            batch_size: batchSize,
            total_requested: totalRequested,
            processed_so_far: processedSoFar,
            current_batch_range: `${offset + 1}-${rangeEnd}`,
            current_batch_symbols: symbols,
            started_at: startedAt,
            elapsed_time: Math.floor((Date.now() - startedAt) / 1000),
          });
          try {
            let batch = null;
            for (let retryAttempt = 0; retryAttempt <= TV_BUSY_MAX_RETRIES; retryAttempt++) {
              try {
                batch = await momentumTvConfirm({ limit: currentLimit, offset, batchNumber, batchSize, timeframes: MOMENTUM_MTF_TIMEFRAMES, save: true, forceUseStaleScores: false });
                break;
              } catch (retryErr) {
                if (isTvManagerBusy(retryErr) && retryAttempt < TV_BUSY_MAX_RETRIES) {
                  console.warn(`Momentum batch ${batchNumber}: TV_MANAGER_BUSY, retry ${retryAttempt + 1}/${TV_BUSY_MAX_RETRIES} after ${TV_BUSY_BASE_DELAY_MS * (retryAttempt + 1)}ms`);
                  await delay(TV_BUSY_BASE_DELAY_MS * (retryAttempt + 1));
                  continue;
                }
                throw retryErr;
              }
            }
            const rows = tvRows(batch);
            processedSoFar += Number(batch?.processed || 0);
            completed.push(batch);
            setMomentumBatchResults([...completed]);
            setLatestMomentumTvRows((current) => [...current, ...rows]);
            setMomentumTvRowsLoaded(true);
            if (offset === 0) {
              try {
                await fetchAndSetTvRuntimeStatusAction({ manual: true });
              } catch (statusErr) {
                console.error("Failed to refresh status after first batch attachment", statusErr);
              }
            }
            setMomentumBatchProgress((current) => current ? { ...current, processed_so_far: processedSoFar, elapsed_time: Math.floor((Date.now() - startedAt) / 1000) } : current);
          } catch (err) {
            if (isRequestCancellation(err)) throw err;
            try {
              await fetchAndSetTvRuntimeStatusAction({ manual: true });
            } catch (statusErr) {
              console.error("Failed to refresh status after batch failure", statusErr);
            }
            setMomentumBatchError({ batch_number: batchNumber, message: formatActionError(err, `momentum batch ${batchNumber}`) });
            setMomentumBatchProgress(null);
            throw err;
          }
          if (momentumBatchStopRef.current) {
            setMomentumBatchStopMessage(`Stopped after Batch ${batchNumber}.`);
            break;
          }
        }
        setMomentumBatchProgress(null);
        try {
          await fetchAndSetTvRuntimeStatusAction({ manual: true });
        } catch (statusErr) {
          console.error("Failed to refresh status after batch completion", statusErr);
        }
        return { total_requested: totalRequested, batch_size: batchSize, batches_completed: completed.length, results: completed };
      } finally {
        tvActionInProgressRef.current = false;
      }
    }),
    momentumStopBatch: () => { momentumBatchStopRef.current = true; setMomentumBatchStopRequested(true); },
    stockMarketData: () => act("stock market data", async () => {
      const searched = normalizeSearchSymbol(search);
      const data = await getMarketDataSymbol({ exchange: searched.exchange, symbol: searched.symbol });
      setStockMarketData(data);
      return data;
    }),
    stockSwingPrecheck: () => act("stock swing precheck", async () => {
      const searched = normalizeSearchSymbol(search);
      const data = await getSwingPrecheck({ exchange: searched.exchange, symbol: searched.symbol });
      setStockSwingPrecheck(data);
      return data;
    }),
    stockMomentumPrecheck: () => act("stock momentum precheck", async () => {
      const searched = normalizeSearchSymbol(search);
      const data = await getMomentumPrecheck({ exchange: searched.exchange, symbol: searched.symbol });
      setStockMomentumPrecheck(data);
      return data;
    }),
    stockSwingTvConfirm: () => act("stock swing tv confirm", async () => {
      const searched = normalizeSearchSymbol(search);
      if (!searched.symbol) throw new Error("Invalid or empty Swing symbol.");
      if (!validateTimeframes(stockSwingTimeframes)) {
        throw new Error("Unsupported timeframe(s) in Swing TV Timeframes. Supported: 1m, 3m, 5m, 15m, 30m, 45m, 1h, 2h, 3h, 4h, 1D, 1W, 1M");
      }
      const data = await swingTvConfirm({ limit: 1, timeframes: stockSwingTimeframes, save: false, singleSymbol: true, symbol: searched.symbol, tradingviewSymbol: searched.tradingview_symbol, exchange: searched.exchange });
      setStockSwingTvResult(data);
      return data;
    }),
    stockMomentumTvConfirm: () => act("stock momentum tv confirm", async () => {
      const searched = normalizeSearchSymbol(search);
      if (!searched.symbol) throw new Error("Invalid or empty Momentum symbol.");
      if (!validateTimeframes(stockMomentumTimeframes)) {
        throw new Error("Unsupported timeframe(s) in Momentum TV Timeframes. Supported: 1m, 3m, 5m, 15m, 30m, 45m, 1h, 2h, 3h, 4h, 1D, 1W, 1M");
      }
      const data = await momentumTvConfirm({ limit: 1, timeframes: stockMomentumTimeframes, save: false, forceUseStaleScores: false, singleSymbol: true, symbol: searched.symbol, tradingviewSymbol: searched.tradingview_symbol, exchange: searched.exchange });
      setStockMomentumTvResult(data);
      return data;
    }),
    tvTest: () => act("tv candles", async () => {
      const searched = normalizeSearchSymbol(tv.symbol);
      if (!searched.symbol) throw new Error("Invalid or empty TradingView symbol.");
      const cleanTf = normalizeTimeframe(tv.timeframe);
      if (!SUPPORTED_TIMEFRAMES.has(cleanTf)) {
        throw new Error("Unsupported TradingView timeframe. Supported: 1m, 3m, 5m, 15m, 30m, 45m, 1h, 2h, 3h, 4h, 1D, 1W, 1M");
      }
      const data = await testTvSymbol(searched.tradingview_symbol || tv.symbol, cleanTf);
      setTvResult(data);
      return data;
    }),
    dryRun750: () => act("dry run 750 scan", async () => { const data = await loadAllMarketData(true); setMarketLoadResult(data); const progress = await getMarketLoadProgress(); setMarketProgress(progress); return { load_all: data, progress }; }),
    scanAll750: () => act("scan all 750 stocks", async () => { const data = await loadAllMarketData(false); setMarketLoadResult(data); const progress = await getMarketLoadProgress(); setMarketProgress(progress); setSwingCandidatesStale(true); setMomentumCandidatesStale(true); setMarketDataNeedsScore(true); setNotice(SCAN_SCORE_WARNING); return { load_all: data, progress }; }),
  };

  const filteredAiFeatureSnapshots = useMemo(() => {
    const rows = Array.isArray(aiFeatureSnapshots) ? aiFeatureSnapshots : [];
    return rows.filter((row) => {
      if (aiDatasetFilters.strategyType && String(row.strategy_type || "").toLowerCase() !== String(aiDatasetFilters.strategyType).toLowerCase()) return false;
      if (aiDatasetFilters.timeframe) {
        const filterTf = normalizeTimeframe(aiDatasetFilters.timeframe);
        if (filterTf && String(row.timeframe || "").toUpperCase() !== filterTf.toUpperCase()) return false;
      }
      return true;
    });
  }, [aiFeatureSnapshots, aiDatasetFilters]);

  const filteredAiOutcomePreview = useMemo(() => {
    if (!aiOutcomePreview) return null;
    const filterTf = aiDatasetFilters.timeframe ? normalizeTimeframe(aiDatasetFilters.timeframe) : "";
    const filterRow = (row) => {
      if (aiDatasetFilters.strategyType && String(row.strategy_type || "").toLowerCase() !== String(aiDatasetFilters.strategyType).toLowerCase()) return false;
      if (filterTf && String(row.timeframe || "").toUpperCase() !== filterTf.toUpperCase()) return false;
      return true;
    };
    const eligible = Array.isArray(aiOutcomePreview.eligible_snapshots) ? aiOutcomePreview.eligible_snapshots : [];
    const skipped = Array.isArray(aiOutcomePreview.skipped_snapshots) ? aiOutcomePreview.skipped_snapshots : [];
    const filteredEligible = eligible.filter(filterRow);
    const filteredSkipped = skipped.filter(filterRow);
    return {
      ...aiOutcomePreview,
      eligible_attach_count: filteredEligible.length,
      eligible_snapshots: filteredEligible,
      skipped_snapshots: filteredSkipped,
    };
  }, [aiOutcomePreview, aiDatasetFilters]);

  const page = useMemo(() => {
    if (activePage === "Swing Trading") return <SwingTrading swingRows={swingRows} swingSummary={swingSummary} latestSwingTvRows={latestSwingTvRows} swingTvRowsLoaded={swingTvRowsLoaded} swingBatchResults={swingBatchResults} swingBatchProgress={swingBatchProgress} swingBatchError={swingBatchError} swingBatchStopRequested={swingBatchStopRequested} swingBatchStopMessage={swingBatchStopMessage} swingBatchRunning={loading === "swing batch tv confirm"} candidatesStale={swingCandidatesStale} metadata={swingSavedTvResult} onSummary={handlers.swingSummary} onLoad={handlers.swing} onBatchConfirm={handlers.swingTvConfirm} onStopBatch={handlers.swingStopBatch} onLoadSaved={handlers.swingSavedTv} onOpenStock={openStockDetail} loading={!!loading} tvRuntimeStatus={tvRuntimeStatus} tvRuntimeLastUpdatedAt={tvRuntimeLastUpdatedAt} onRefreshTvStatus={handlers.tvRefreshStatus} />;
    if (activePage === "Momentum Trading") return <MomentumTrading momentumRows={momentumRows} momentumSummary={momentumSummary} latestMomentumTvRows={latestMomentumTvRows} momentumTvRowsLoaded={momentumTvRowsLoaded} momentumBatchResults={momentumBatchResults} momentumBatchProgress={momentumBatchProgress} momentumBatchError={momentumBatchError} momentumBatchStopRequested={momentumBatchStopRequested} momentumBatchStopMessage={momentumBatchStopMessage} momentumBatchRunning={loading === "momentum batch tv confirm"} candidatesStale={momentumCandidatesStale} metadata={momentumSavedTvResult} onSummary={handlers.momentumSummary} onLoad={handlers.momentum} onBatchConfirm={handlers.momentumBatchConfirm} onStopBatch={handlers.momentumStopBatch} onLoadSaved={handlers.momentumSavedTv} onOpenStock={openStockDetail} loading={!!loading} tvRuntimeStatus={tvRuntimeStatus} tvRuntimeLastUpdatedAt={tvRuntimeLastUpdatedAt} onRefreshTvStatus={handlers.tvRefreshStatus} />;
    if (activePage === "Market Data") return <MarketDataPage tv={tv} setTv={setTv} tvResult={tvResult} onTest={handlers.tvTest} onDryRun750={handlers.dryRun750} onScanAll750={handlers.scanAll750} onScoreMarketData={handlers.scoreMarketData} marketLoadResult={marketLoadResult} marketProgress={marketProgress} scoreRunResult={scoreRunResult} scoreSummary={scoreSummary} marketDataNeedsScore={marketDataNeedsScore} loading={!!loading} loadingText={loading} lastResponse={lastResponse} />;
    if (activePage === "Stock Detail") return <StockDetailPage search={search} stockMarketData={stockMarketData} stockSwingPrecheck={stockSwingPrecheck} stockMomentumPrecheck={stockMomentumPrecheck} stockSwingTvResult={stockSwingTvResult} stockMomentumTvResult={stockMomentumTvResult} stockSavedSwingResult={stockSavedSwingResult} stockSavedMomentumResult={stockSavedMomentumResult} latestSwingTvRows={latestSwingTvRows} latestMomentumTvRows={latestMomentumTvRows} stockSwingTimeframes={stockSwingTimeframes} setStockSwingTimeframes={setStockSwingTimeframes} stockMomentumTimeframes={stockMomentumTimeframes} setStockMomentumTimeframes={setStockMomentumTimeframes} onLoadStockMarket={handlers.stockMarketData} onSwingPrecheck={handlers.stockSwingPrecheck} onMomentumPrecheck={handlers.stockMomentumPrecheck} onStockSwingTvConfirm={handlers.stockSwingTvConfirm} onStockMomentumTvConfirm={handlers.stockMomentumTvConfirm} loading={!!loading} />;
    if (activePage === "Paper Trades") return <PaperTrades openTrades={paperOpenTrades} history={paperHistory} summary={summary} liveStatus={paperLiveStatus} />;
    if (activePage === "Settings") return <Settings settings={settings} health={health} runtimeInfo={systemRuntimeInfo} tvRuntimeStatus={tvRuntimeStatus} tvAttachableTabs={tvAttachableTabs} onRefreshTvTabs={handlers.tvRefreshTabs} onAttachTvTab={handlers.tvAttachTab} onDetachTvTab={handlers.tvDetachTab} loading={!!loading} />;
    if (activePage === "Data Collection") return <DataCollectionPage collectionStatus={aiDataCollectionStatus} summary={aiDatasetSummary} />;
    if (activePage === "AI Dataset / Labels") return <AiDatasetPage summary={aiDatasetSummary} snapshots={filteredAiFeatureSnapshots} outcomePreview={filteredAiOutcomePreview} filters={aiDatasetFilters} onFiltersChange={setAiDatasetFilters} onRefresh={handlers.aiDatasetSummary} loading={loading === "AI dataset summary"} errors={aiDatasetErrors} />;
    if (activePage === "System Health") return <SystemHealthPage health={health} tvRuntimeStatus={tvRuntimeStatus} schedulerStatus={paperUpdateScheduler} runs={paperUpdateRuns} />;

    return <Dashboard summary={summary} scoreSummary={scoreSummary} swingSummary={swingSummary} momentumSummary={momentumSummary} dashboardEquity={dashboardEquity} health={health} tvRuntimeStatus={tvRuntimeStatus} aiDatasetSummary={aiDatasetSummary} aiFeatureSnapshots={filteredAiFeatureSnapshots} aiOutcomePreview={filteredAiOutcomePreview} aiDataCollectionStatus={aiDataCollectionStatus} aiDatasetFilters={aiDatasetFilters} aiDatasetErrors={aiDatasetErrors} dashboardErrors={dashboardErrors} paperUpdateProgress={paperUpdateProgress} paperUpdateRuns={paperUpdateRuns} paperUpdateLock={paperUpdateLock} paperUpdateScheduler={paperUpdateScheduler} onSummary={handlers.loadSummary} onDryRun={handlers.dryRun} onSaveRun={handlers.saveRun} onAiDatasetRefresh={handlers.aiDatasetSummary} onAiDatasetFiltersChange={setAiDatasetFilters} aiDatasetLoading={loading === "AI dataset summary"} loading={!!loading} />;
  }, [activePage, settings, health, systemRuntimeInfo, summary, scoreSummary, swingSummary, momentumSummary, dashboardEquity, tvRuntimeStatus, tvRuntimeLastUpdatedAt, tvAttachableTabs, aiDatasetSummary, filteredAiFeatureSnapshots, filteredAiOutcomePreview, aiDataCollectionStatus, aiDatasetFilters, aiDatasetErrors, dashboardErrors, paperUpdateProgress, paperUpdateRuns, paperUpdateLock, paperUpdateScheduler, swingRows, latestSwingTvRows, swingTvRowsLoaded, swingBatchResults, swingBatchProgress, swingBatchError, swingBatchStopRequested, swingBatchStopMessage, swingCandidatesStale, momentumRows, momentumCandidatesStale, latestMomentumTvRows, momentumTvRowsLoaded, momentumBatchResults, momentumBatchProgress, momentumBatchError, momentumBatchStopRequested, momentumBatchStopMessage, stockMarketData, stockSwingPrecheck, stockMomentumPrecheck, stockSwingTvResult, stockMomentumTvResult, stockSavedSwingResult, stockSavedMomentumResult, stockSwingTimeframes, stockMomentumTimeframes, search, tv, tvResult, marketLoadResult, marketProgress, scoreRunResult, marketDataNeedsScore, paperOpenTrades, paperHistory, paperLiveStatus, loading, lastResponse]);

  const tvBadge = tradingViewBadge(tvRuntimeStatus);

  return <div className="appShell">
    <aside className="sidebar"><div className="brand"><div className="brandMark">TA</div><div><h1>Trading Agent</h1><p>Paper Terminal</p></div></div><nav>
      {["MAIN", "TRADING", "DATA / AI", "SYSTEM"].map(groupName => (
        <React.Fragment key={groupName}>
          <div className="navSeparator">{groupName}</div>
          {NAV_WITH_STOCK_DETAIL.filter(item => item.group === groupName).map((item) => (
            <button className={activePage === item.label ? "navItem active" : "navItem"} key={item.label} onClick={() => setActivePage(item.label)}><span>{item.icon}</span>{item.label}</button>
          ))}
        </React.Fragment>
      ))}
    </nav><div className="sidebarFooter"><Badge tone="yellow">PAPER ONLY</Badge><p>No live trading. No broker orders.</p></div></aside>
    <div className="mainArea">
      <header className="topHeader"><div><h2>{activePage}</h2><p>API base: {API_BASE}</p></div><input className="searchInput" value={search} onChange={(e) => { setSearch(e.target.value); if (e.target.value.trim()) setActivePage("Stock Detail"); }} placeholder="Search NSE/BSE symbols..." /><div className="statusBadges"><Badge tone={health.online ? "green" : "red"}>Market API {health.status}</Badge><Badge tone={tvBadge.tone}>{tvBadge.label}</Badge><Badge tone="yellow">Paper Mode</Badge></div></header>
      <section className="modeBanner"><strong>PAPER MODE / NO LIVE ORDERS</strong><span>Simulated signals and paper trade plans only.</span></section>
      {loading && <div className="noticePanel">Loading {loading}...</div>}
      {notice && <div className="warningText appNotice">{notice}</div>}
      {error && <div className="errorPanel">{error}</div>}
      <main className="pageContent">
        <PageErrorBoundary key={activePage} pageKey={activePage}>
          {page}
        </PageErrorBoundary>
      </main>
    </div>
  </div>;
}
