import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  API_BASE, approvePaperUpdateFromDryRun, buildMomentumSignals, buildPaperPlans, buildSwingSignals, getActiveTrades,
  getAiFeatureDatasetSummary, getAiFeatureSnapshots, getAiOutcomePreview, getAllTrades, getHealth, getMarketDataSymbol, getMarketLoadProgress, getMomentumCandidates, getMomentumPrecheck, getPaperPlans, getPaperSignals,
  getMomentumSummary, getMomentumTvConfirmed, getPaperEquity, getPaperSummary, getPaperUpdateLock, getPaperUpdateProgress, getPaperUpdateRuns, getPaperUpdateSchedulerStatus, getScanRows, getScoreSummary, getSettings, getSwingCandidates,
  getSwingPrecheck, getSwingSummary, getSwingTvConfirmed, momentumTvConfirm, loadAllMarketData, runPaperPipeline, runScan, runScoring,
  runPaperUpdateDryRun, swingTvConfirm, testTvSymbol, updatePaperPlans,
} from "./api";
import { aiDataCollectionChecklist, aiOutcomeEligibleRows, aiOutcomeSkippedRows, aiSnapshotDisplayRows } from "./aiDataset";
import { canApprovePaperRealUpdate } from "./paperRealUpdateApproval";

const NAV_ITEMS = [
  { label: "Dashboard", icon: "◆" },
  { label: "Swing Trading", icon: "↗" },
  { label: "Momentum Trading", icon: "▲" },
  { label: "Market Data", icon: "⌕" },
  { label: "Paper Trades", icon: "▣" },
  { label: "Settings", icon: "⚙" },
];

const NAV_WITH_STOCK_DETAIL = NAV_ITEMS.some((item) => item.label === "Stock Detail")
  ? NAV_ITEMS
  : [...NAV_ITEMS.slice(0, 4), { label: "Stock Detail", icon: "S" }, ...NAV_ITEMS.slice(4)];

const arr = (value, keys = []) => {
  if (Array.isArray(value)) return value;
  const key = keys.find((name) => Array.isArray(value?.[name]));
  return key ? value[key] : [];
};
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
const paperUpdateProposalRows = (result) => {
  const directKeys = ["results", "per_trade_results", "proposals", "trades"];
  for (const key of directKeys) {
    if (Array.isArray(result?.[key])) return result[key];
  }
  const details = result?.details;
  for (const key of directKeys) {
    if (Array.isArray(details?.[key])) return details[key];
  }
  return [];
};
const normalizeDryRunForApproval = (dryRunResult, progress, runs) => {
  if (dryRunResult === null || typeof dryRunResult !== "object") return dryRunResult;
  const runId = dryRunResult.run_id;
  const runRows = Array.isArray(runs) ? runs : [];
  const matchingProgress = runId && progress?.run_id === runId ? progress : null;
  const matchingRun = runRows.find((run) => run?.run_id === runId) || null;
  const fallback = matchingRun || matchingProgress || {};
  return {
    ...dryRunResult,
    status: dryRunResult.status ?? fallback.status,
    dry_run: dryRunResult.dry_run ?? fallback.dry_run,
    mongo_writes_enabled: dryRunResult.mongo_writes_enabled ?? fallback.mongo_writes_enabled,
    paper_only: dryRunResult.paper_only ?? fallback.paper_only,
    live_trading: dryRunResult.live_trading ?? fallback.live_trading,
    broker_orders: dryRunResult.broker_orders ?? fallback.broker_orders,
    errors_count: dryRunResult.errors_count ?? fallback.errors_count,
    blocked: dryRunResult.blocked ?? fallback.blocked,
    proposed_write_count: dryRunResult.proposed_write_count ?? fallback.proposed_write_count,
    max_trades: dryRunResult.max_trades ?? fallback.max_trades,
    max_writes: dryRunResult.max_writes ?? fallback.max_writes,
    finished_at: dryRunResult.finished_at ?? fallback.finished_at,
  };
};
const paperUpdateChangedTradeIds = (result) => {
  if (Array.isArray(result?.changed_trade_ids)) return result.changed_trade_ids;
  if (Array.isArray(result?.details?.changed_trade_ids)) return result.details.changed_trade_ids;
  return [];
};
const paperUpdateApprovalRejectionReason = (result) => (
  result?.block_reason
  || result?.rejection_code
  || result?.code
  || result?.details?.block_reason
  || result?.details?.rejection_code
  || result?.details?.reason
  || result?.error
  || result?.message
  || "-"
);
function PaperUpdateDryRunWarnings({ result }) {
  if (!result) return null;
  const warnings = [];
  const maxWrites = Number(result?.max_writes ?? 1);
  const proposedWrites = Number(result?.proposed_write_count ?? 0);
  const errorsCount = Number(result?.errors_count ?? (Array.isArray(result?.errors) ? result.errors.length : 0));
  if (result?.mongo_writes_enabled !== false) warnings.push("UNSAFE: mongo_writes_enabled is not false during dry-run.");
  if (result?.live_trading === true) warnings.push("UNSAFE: live_trading is true.");
  if (result?.broker_orders === true) warnings.push("UNSAFE: broker_orders is true.");
  if (proposedWrites > maxWrites) warnings.push(`Blocked warning: proposed_write_count ${proposedWrites} exceeds max_writes ${maxWrites}.`);
  if (errorsCount > 0) warnings.push(`Error warning: dry-run reported ${errorsCount} error(s).`);
  if (!warnings.length) return <div className="safeNotice">Dry-run safety flags are safe: no Mongo writes, no live trading, no broker orders.</div>;
  return <div className="safetyWarningList">{warnings.map((warning) => <div key={warning}>{warning}</div>)}</div>;
}
function PaperUpdateDryRunResult({ result }) {
  if (!result) return null;
  const rows = paperUpdateProposalRows(result);
  const summaryItems = [
    ["processed", result?.processed],
    ["proposed_write_count", result?.proposed_write_count],
    ["updated_count", result?.updated_count],
    ["errors_count", result?.errors_count],
    ["blocked", boolLabel(result?.blocked)],
    ["block_reason", result?.block_reason || "-"],
    ["max_trades", result?.max_trades],
    ["max_writes", result?.max_writes],
    ["mongo_writes_enabled", boolLabel(result?.mongo_writes_enabled)],
    ["paper_only", boolLabel(result?.paper_only)],
    ["live_trading", boolLabel(result?.live_trading)],
    ["broker_orders", boolLabel(result?.broker_orders)],
  ];
  const columns = ["symbol", "current status", "proposed status", "current outcome", "proposed outcome", "proposed P&L", "reason", "would_write", "write_attempted"];
  return <div className="paperUpdateResultPanel">
    <h3>Latest Manual Dry-Run Result</h3>
    <PaperUpdateDryRunWarnings result={result} />
    <div className="safetyGrid">
      {summaryItems.map(([label, value]) => <SafetyMetric key={label} label={label} value={value ?? "-"} tone={label.includes("enabled") || label.includes("live") || label.includes("broker") ? boolTone(value === "true", true) : "gray"} />)}
    </div>
    <div className="tableShell results-table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>
      {rows.length ? rows.map((row, index) => {
        const values = {
          symbol: row?.symbol || row?.tradingview_symbol || row?.canonical_symbol || "-",
          "current status": row?.previous_status ?? row?.current_status ?? row?.status,
          "proposed status": row?.proposed_new_status ?? row?.proposed_status ?? row?.status,
          "current outcome": row?.previous_outcome_status ?? row?.current_outcome_status ?? row?.outcome_status,
          "proposed outcome": row?.proposed_new_outcome_status ?? row?.proposed_outcome_status ?? row?.outcome_status,
          "proposed P&L": row?.proposed_pnl ?? row?.paper_pnl,
          reason: row?.proposed_reason ?? row?.reason ?? row?.error_message,
          would_write: boolLabel(row?.would_write),
          write_attempted: boolLabel(row?.write_attempted),
        };
        return <tr key={`${values.symbol}-${index}`}>{columns.map((column) => (
          <td key={column} className={column === "reason" ? "wideText" : ""}>{fmt(values[column])}</td>
        ))}</tr>;
      }) : <tr><td colSpan={columns.length}>No per-trade dry-run proposals returned.</td></tr>}
    </tbody></table></div>
  </div>;
}
function PaperUpdateApprovalResult({ result, error }) {
  if (!result && !error) return null;
  const rows = paperUpdateProposalRows(result);
  const changedTradeIds = paperUpdateChangedTradeIds(result);
  const rejectionReason = paperUpdateApprovalRejectionReason(result);
  const summaryItems = [
    ["approved_dry_run_id", result?.approved_dry_run_id],
    ["real_run_id", result?.real_run_id ?? result?.run_id],
    ["updated_count", result?.updated_count],
    ["successful_updates_count", result?.successful_updates_count],
    ["errors_count", result?.errors_count],
    ["blocked", boolLabel(result?.blocked)],
    ["block_reason / rejection code", rejectionReason],
    ["paper_only", boolLabel(result?.paper_only)],
    ["live_trading", boolLabel(result?.live_trading)],
    ["broker_orders", boolLabel(result?.broker_orders)],
  ];
  const columns = ["trade_id", "symbol", "updated", "write_attempted", "status", "outcome_status", "applied_update", "error"];
  return <div className="paperUpdateResultPanel">
    <h3>Latest Approval Result</h3>
    {error && <div className="safetyWarningList"><div>{error}</div></div>}
    {result?.blocked && <div className="safetyWarningList"><div>Approval rejected: {rejectionReason}. Run a fresh dry-run before trying approval again.</div></div>}
    {result && <div className="safetyGrid">
      {summaryItems.map(([label, value]) => <SafetyMetric key={label} label={label} value={value ?? "-"} tone={label.includes("live") || label.includes("broker") ? boolTone(value === "true", true) : result?.blocked ? "yellow" : "gray"} />)}
    </div>}
    {result && <div className="approvalChangedIds">
      <span>changed trade IDs</span>
      <strong>{changedTradeIds.length ? changedTradeIds.join(", ") : "-"}</strong>
    </div>}
    {result && <div className="tableShell results-table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>
      {rows.length ? rows.map((row, index) => {
        const values = {
          trade_id: row?.trade_id ?? row?.id ?? "-",
          symbol: row?.symbol || row?.tradingview_symbol || row?.canonical_symbol || "-",
          updated: boolLabel(row?.updated),
          write_attempted: boolLabel(row?.write_attempted),
          status: row?.status ?? row?.applied_update?.status ?? row?.proposed_new_status ?? row?.proposed_status,
          outcome_status: row?.outcome_status ?? row?.applied_update?.outcome_status ?? row?.proposed_new_outcome_status ?? row?.proposed_outcome_status,
          applied_update: row?.applied_update ?? row?.proposed_update,
          error: row?.error || row?.error_message || row?.reason || row?.proposed_reason,
        };
        return <tr key={`${values.trade_id}-${values.symbol}-${index}`}>{columns.map((column) => (
          <td key={column} className={column === "applied_update" || column === "error" ? "wideText" : ""}>{fmt(values[column])}</td>
        ))}</tr>;
      }) : <tr><td colSpan={columns.length}>No per-trade approval results returned.</td></tr>}
    </tbody></table></div>}
  </div>;
}
function PaperUpdateApprovalPanel({
  approvalDryRun,
  lockStatus,
  schedulerStatus,
  confirmationText,
  onConfirmationTextChange,
  onApprove,
  approvalLoading,
  approvalResult,
  approvalError,
  actionDisabled,
}) {
  const gate = useMemo(() => canApprovePaperRealUpdate({
    latestDryRun: approvalDryRun,
    lockStatus,
    schedulerStatus,
  }), [approvalDryRun, lockStatus, schedulerStatus]);
  const confirmationMatches = confirmationText === PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT;
  const canApprove = gate.allowed && confirmationMatches;
  const handleConfirmationKeyDown = (event) => {
    if (event.key === "Enter") event.preventDefault();
  };
  return <div className="approvalPanel">
    <div className="approvalHeader">
      <div><span>manual bound approval</span><h3>Approve One Paper Update</h3></div>
      <Badge tone={gate.allowed ? "green" : "yellow"}>{gate.allowed ? "gate passed" : "gate blocked"}</Badge>
    </div>
    <p className="muted">This button can only call the backend approval binding for the latest approved dry-run ID. It does not call unbound real update paths.</p>
    {gate.reasons.length ? <div className="safetyWarningList">{gate.reasons.map((reason) => <div key={reason}>{reason}</div>)}</div> : <div className="safeNotice">Approval gate passed. Exact typed confirmation is still required.</div>}
    <label className="approvalConfirmLabel">
      <span>Type this exact confirmation before approval:</span>
      <code>{PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT}</code>
      <textarea
        rows={2}
        value={confirmationText}
        onChange={(event) => onConfirmationTextChange(event.target.value)}
        onKeyDown={handleConfirmationKeyDown}
        placeholder={PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT}
        spellCheck="false"
      />
    </label>
    {!confirmationMatches && <p className="approvalHint">Approval stays disabled until the confirmation text matches exactly. Pressing Enter in this box is ignored; approval requires a click.</p>}
    <ActionButton onClick={onApprove} disabled={actionDisabled || approvalLoading || !canApprove}>Approve One Paper Update</ActionButton>
    <PaperUpdateApprovalResult result={approvalResult} error={approvalError} />
  </div>;
}
function PaperUpdateSafety({
  progress,
  schedulerStatus,
  lockStatus,
  dryRunResult,
  updateRuns,
  onRunDryRun,
  dryRunLoading,
  onApproveDryRun,
  approvalLoading,
  approvalText,
  onApprovalTextChange,
  approvalResult,
  approvalError,
  actionDisabled,
}) {
  const lock = lockStatus || schedulerStatus?.lock || {};
  const schedulerEnabled = schedulerStatus?.enabled;
  const schedulerRunning = schedulerStatus?.scheduler_running;
  const autoEnabled = schedulerStatus?.automatic_updates_enabled;
  const approvalDryRun = normalizeDryRunForApproval(dryRunResult, progress, updateRuns);
  const schedulerWarnings = [
    schedulerStatus?.unsafe_warning,
    schedulerStatus?.emergency_warning && schedulerStatus?.emergency_warning !== schedulerStatus?.unsafe_warning
      ? schedulerStatus.emergency_warning
      : null,
  ].filter(Boolean);
  const unsafeReasons = Array.isArray(schedulerStatus?.unsafe_reasons) ? schedulerStatus.unsafe_reasons : [];
  return <Card title="Paper Update Safety / Automation Status" eyebrow="read-only">
    <div className="safetyActions">
      <ActionButton onClick={onRunDryRun} disabled={actionDisabled || dryRunLoading}>{dryRunLoading ? "Running Paper Update Dry-Run..." : "Run Paper Update Dry-Run"}</ActionButton>
      <p className="muted">Dry-run only. It evaluates existing paper trades and must not write to MongoDB, place orders, run scans, scoring, market loading, pipeline generation, or TV confirmation.</p>
    </div>
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
    <PaperUpdateDryRunResult result={dryRunResult} />
    <PaperUpdateApprovalPanel
      approvalDryRun={approvalDryRun}
      lockStatus={lockStatus}
      schedulerStatus={schedulerStatus}
      confirmationText={approvalText}
      onConfirmationTextChange={onApprovalTextChange}
      onApprove={onApproveDryRun}
      approvalLoading={approvalLoading}
      approvalResult={approvalResult}
      approvalError={approvalError}
      actionDisabled={actionDisabled}
    />
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

function AiDatasetSummary({ summary, snapshots, outcomePreview, filters, onFiltersChange, onRefresh, loading }) {
  const readyForModelTraining = summary?.ready_for_model_training === true;
  const readinessReasons = Array.isArray(summary?.readiness_reason) ? summary.readiness_reason : [];
  const checklist = aiDataCollectionChecklist(summary, outcomePreview);
  return <Card title="AI Dataset Summary" eyebrow="read-only dataset tracking">
    <div className="warningText">{readyForModelTraining ? "Dataset readiness checks passed. No AI model is running." : "AI model training is not ready yet."}</div>
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

function Dashboard({
  summary,
  scoreSummary,
  swingSummary,
  momentumSummary,
  aiDatasetSummary,
  aiFeatureSnapshots,
  aiOutcomePreview,
  aiDatasetFilters,
  paperUpdateProgress,
  paperUpdateRuns,
  paperUpdateLock,
  paperUpdateScheduler,
  paperUpdateDryRunResult,
  paperUpdateApprovalText,
  paperUpdateApprovalResult,
  paperUpdateApprovalError,
  onSummary,
  onDryRun,
  onSaveRun,
  onPaperUpdateDryRun,
  onPaperUpdateApprove,
  onPaperUpdateApprovalTextChange,
  onAiDatasetFiltersChange,
  onAiDatasetRefresh,
  aiDatasetLoading,
  paperUpdateDryRunLoading,
  paperUpdateApprovalLoading,
  loading,
}) {
  return <div className="pageStack">
    <section className="heroCard">
      <div><span>Paper control room</span><h1>Indian Stock Trading Assistant</h1><p>Swing and momentum workflows powered by TradingView candles. Paper records only.</p></div>
      <div className="heroActions"><ActionButton onClick={onSummary} disabled={loading}>Load Summary</ActionButton><ActionButton onClick={onDryRun} disabled={loading}>Pipeline Dry Run</ActionButton><ActionButton onClick={onSaveRun} disabled={loading}>Save Paper Run</ActionButton></div>
    </section>
    <div className="warningText">Save mode updates PAPER records only. No broker orders. No live trading.</div>
    <div className="statsGrid">
      <StatCard label="Total Paper Trades" value={summary?.total_trades ?? "--"} />
      <StatCard label="Open Trades" value={summary?.open_trades ?? "--"} />
      <StatCard label="Closed Trades" value={summary?.closed_trades ?? "--"} tone="yellow" />
      <StatCard label="Total Paper P&L" value={summary?.total_paper_pnl ?? "--"} />
      <StatCard label="Win Rate" value={summary?.win_rate_percent ?? "--"} tone="yellow" />
      <StatCard label="Active Symbols" value={Array.isArray(summary?.symbols) ? summary.symbols.length : "--"} />
    </div>
    <div className="statsGrid compact">
      <StatCard label="Scored Rows" value={scoreSummary?.total_scored ?? "--"} />
      <StatCard label="Swing Candidates" value={swingSummary?.swing_candidates_count ?? scoreSummary?.swing_candidates_count ?? "--"} tone="yellow" />
      <StatCard label="Momentum Candidates" value={momentumSummary?.momentum_candidates_count ?? scoreSummary?.momentum_candidates_count ?? "--"} />
    </div>
    <AiDatasetSummary summary={aiDatasetSummary} snapshots={aiFeatureSnapshots} outcomePreview={aiOutcomePreview} filters={aiDatasetFilters} onFiltersChange={onAiDatasetFiltersChange} onRefresh={onAiDatasetRefresh} loading={aiDatasetLoading} />
    <PaperUpdateSafety
      progress={paperUpdateProgress}
      schedulerStatus={paperUpdateScheduler}
      lockStatus={paperUpdateLock}
      dryRunResult={paperUpdateDryRunResult}
      updateRuns={paperUpdateRuns}
      onRunDryRun={onPaperUpdateDryRun}
      dryRunLoading={paperUpdateDryRunLoading}
      onApproveDryRun={onPaperUpdateApprove}
      approvalLoading={paperUpdateApprovalLoading}
      approvalText={paperUpdateApprovalText}
      onApprovalTextChange={onPaperUpdateApprovalTextChange}
      approvalResult={paperUpdateApprovalResult}
      approvalError={paperUpdateApprovalError}
      actionDisabled={loading}
    />
    <PaperUpdateRunHistory runs={paperUpdateRuns} />
    <div className="threeGrid">
      <Card title="Recent Activity" eyebrow="paper log"><div className="activityList"><p>Summary ready</p><p>TradingView candles available</p><p>Pipeline dry-run enabled</p></div></Card>
      <Card title="Swing Strategy" eyebrow="score > 80"><div className="strategyCard"><strong>Confirmation-first swing flow</strong><Badge tone="green">Paper plans</Badge></div></Card>
      <Card title="Momentum Strategy" eyebrow="momentum >= 70"><div className="strategyCard"><strong>Volume and 20D high validation</strong><Badge tone="yellow">Watch mode</Badge></div></Card>
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
const PAPER_UPDATE_DRY_RUN_CONFIRM = [
  "Run Paper Update Dry-Run?",
  "",
  "This calls dry_run=true only.",
  "It will not write to paper_trades or MongoDB.",
  "It will not place broker/live orders.",
  "It will not run scan, scoring, market loading, paper pipeline generation, or TradingView confirmation endpoints.",
].join("\n");
const PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT = "I understand this will write to paper_trades only and will not place broker orders";
const PAPER_UPDATE_APPROVAL_MAX_TRADES = 6;
const PAPER_UPDATE_APPROVAL_MAX_WRITES = 1;

function formatActionError(err, actionName = "request") {
  const status = err?.status ? `HTTP ${err.status}` : "HTTP status unavailable";
  const message = err?.message || String(err);
  const body = err?.responseBody ? JSON.stringify(err.responseBody, null, 2) : err?.rawBody || "";
  const actionText = String(actionName || "").toLowerCase();
  const shouldShowTvHint = actionText.includes("tv") || /tradingview|debug|connection|fetch|timeout|network/i.test(`${message} ${body}`);
  return `${status}\nMessage: ${message}${body ? `\nResponse body:\n${body}` : ""}${shouldShowTvHint ? `\nHint: ${TRADINGVIEW_ERROR_HINT}` : ""}`;
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
  if (!raw) return { exchange: "NSE", symbol: "", tradingview_symbol: "" };
  if (raw.includes(":")) {
    const [exchange, ...parts] = raw.split(":");
    const symbol = parts.join(":").trim();
    const cleanExchange = exchange.trim() || "NSE";
    return { exchange: cleanExchange, symbol, tradingview_symbol: symbol ? `${cleanExchange}:${symbol}` : "" };
  }
  return { exchange: "NSE", symbol: raw, tradingview_symbol: `NSE:${raw}` };
}

function firstRow(data) {
  return data?.row || tvRows(data)[0] || {};
}

const SWING_MTF_TIMEFRAMES = "1W,1D,4H,1H";
const MOMENTUM_MTF_TIMEFRAMES = "1D,4H,1H";
const SWING_TV_BATCH_SIZE = 10;
const MOMENTUM_TV_BATCH_SIZE = 10;
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
  const tv = String(row?.requested_tradingview_symbol || row?.tradingview_symbol || "").trim().toUpperCase();
  const symbol = String(row?.symbol || row?.canonical_symbol || "").trim().toUpperCase();
  return tv || (symbol ? `NSE:${symbol}` : "");
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
      <span>{fmt(row?.confidence_score ?? row?.score ?? row?.momentum_score)} confidence</span>
      <span>{shortReason}</span>
      {isMomentum && row?.trap_status && <span>trap: {row.trap_status}</span>}
      {failed.length > 0 && <span>failed: {failed.join(", ")}</span>}
    </div>
    {row?.weekly_history_warning && <p className="savedResultWarning">Weekly data insufficient. Daily used as higher-timeframe backup.</p>}
  </div>;
}

function TvResultSummaryPanel({ title, rows, mode, loaded, onOpenStock }) {
  const [openSection, setOpenSection] = useState(null);
  const confirmedStatuses = mode === "momentum" ? MOMENTUM_CONFIRMED_STATUSES : SWING_CONFIRMED_STATUSES;
  const waitWatchStatuses = mode === "momentum" ? MOMENTUM_WAIT_WATCH_STATUSES : SWING_WAIT_WATCH_STATUSES;
  const safeRows = Array.isArray(rows) ? rows : [];
  const confirmedRows = safeRows.filter((row) => confirmedStatuses.has(savedStatus(row)));
  const waitWatchRows = safeRows.filter((row) => waitWatchStatuses.has(savedStatus(row)));
  const failedRows = sortByTradeQuality(safeRows.filter((row) => FAILED_STATUSES.has(savedStatus(row))));
  const confirmedWatchRows = sortByTradeQuality([...confirmedRows, ...waitWatchRows]);
  const activeRows = openSection === "confirmed" ? confirmedWatchRows : openSection === "failed" ? failedRows : [];
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
      <button className={`savedSummaryCard ${openSection === "failed" ? "active" : ""}`} type="button" onClick={() => setOpenSection(openSection === "failed" ? null : "failed")}>
        <span>Failed</span><strong>{failedRows.length}</strong>
      </button>
    </div>
    <p className="muted">{mode === "momentum" ? "Momentum" : "Swing"} TV rows available: {safeRows.length}</p>
    {safeRows.length === 0 && <p className="muted">No saved TV rows found.</p>}
    {openSection && <div className="savedResultList">
      {activeRows.length ? activeRows.map((row, index) => <SavedResultCard key={`${row?.symbol || row?.tradingview_symbol || openSection}-${index}`} row={row} mode={mode} onOpenStock={onOpenStock} />) : <p className="muted">No {openSection} TV results.</p>}
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
  onSignals,
  onPlans,
  onOpenStock,
  loading,
}) {
  const [selectedSwingRow, setSelectedSwingRow] = useState(null);
  const resultRows = sortByTradeQuality(flattenMtfRows(latestSwingTvRows));
  const sortedSwingRows = [...(Array.isArray(swingRows) ? swingRows : [])].sort((a, b) => Number(b?.score || 0) - Number(a?.score || 0));
  const selectedRow = selectedSwingRow;
  const openSwingStock = (row) => {
    setSelectedSwingRow(row);
    onOpenStock?.(row);
  };
  return <div className="pageStack">
    <div className="warningText">TradingView confirmation only. No broker orders. No live trading.</div>
    {candidatesStale && <div className="warningText">Swing candidates may be stale. Run Score Market Data.</div>}
    <div className="tradingActionPanel">
      <div className="buttonRow tradingButtonRow">
        <ActionButton onClick={onSummary} disabled={loading}>Load Swing Summary</ActionButton>
        <ActionButton onClick={onLoad} disabled={loading}>Load Swing Candidates</ActionButton>
      </div>
      <div className="buttonRow tradingButtonRow tradingBatchActions">
        <ActionButton onClick={onBatchConfirm} disabled={loading}>Run Swing TV Confirm in Batches</ActionButton>
        <ActionButton onClick={onStopBatch} disabled={!swingBatchRunning || swingBatchStopRequested}>Stop After Current Batch</ActionButton>
        <ActionButton onClick={onLoadSaved} disabled={loading}>Load Saved Swing TV Results</ActionButton>
      </div>
    </div>
    <BatchProgressNotice progress={swingBatchProgress} />
    {swingBatchStopRequested && !swingBatchStopMessage && <div className="warningText">Stop requested. Current batch will finish, then processing will stop.</div>}
    {swingBatchStopMessage && <div className="warningText">{swingBatchStopMessage}</div>}
    {swingBatchError && <div className="errorPanel">Batch {swingBatchError.batch_number} failed: {swingBatchError.message}</div>}
    <SummaryCards data={swingSummary} fields={["total_scored", "swing_candidates_count", "below_threshold_count", "invalid_count", "top_score"]} />
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
    <TvResultSummaryPanel title="Saved / Latest Swing TV Results" rows={resultRows} mode="swing" loaded={swingTvRowsLoaded} onOpenStock={onOpenStock} />
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
  onSignals,
  onPlans,
  onOpenStock,
  loading,
}) {
  const [selectedMomentumRow, setSelectedMomentumRow] = useState(null);
  const sortedMomentumRows = [...(Array.isArray(momentumRows) ? momentumRows : [])].sort((a, b) => Number(b?.momentum_score || 0) - Number(a?.momentum_score || 0));
  const selectedRow = selectedMomentumRow;
  const resultRows = sortByTradeQuality(flattenMomentumMtfRows(latestMomentumTvRows));
  const openMomentumStock = (row) => {
    setSelectedMomentumRow(row);
    onOpenStock?.(row);
  };
  return <div className="pageStack">
    <div className="warningText">Candidates are scanner outputs only. No broker orders. No live trading.</div>
    {candidatesStale && <div className="warningText">{MOMENTUM_SCORE_STALE_MESSAGE}</div>}
    <div className="tradingActionPanel">
      <div className="buttonRow tradingButtonRow">
        <ActionButton onClick={onSummary} disabled={loading}>Load Momentum Summary</ActionButton>
        <ActionButton onClick={onLoad} disabled={loading}>Load Momentum Candidates</ActionButton>
      </div>
      <div className="buttonRow tradingButtonRow tradingBatchActions">
        <ActionButton onClick={onBatchConfirm} disabled={loading}>Run Momentum TV Confirm in Batches</ActionButton>
        <ActionButton onClick={onStopBatch} disabled={!momentumBatchRunning || momentumBatchStopRequested}>Stop After Current Batch</ActionButton>
        <ActionButton onClick={onLoadSaved} disabled={loading}>Load Saved Momentum TV Results</ActionButton>
      </div>
    </div>
    <BatchProgressNotice progress={momentumBatchProgress} />
    {momentumBatchStopRequested && !momentumBatchStopMessage && <div className="warningText">Stop requested. Current batch will finish, then processing will stop.</div>}
    {momentumBatchStopMessage && <div className="warningText">{momentumBatchStopMessage}</div>}
    {momentumBatchError && <div className="errorPanel">Batch {momentumBatchError.batch_number} failed: {momentumBatchError.message}</div>}
    <SummaryCards data={momentumSummary} fields={["total_scored", "momentum_candidates_count", "below_threshold_count", "overextended_count", "invalid_count", "top_momentum_score"]} />
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
    <TvResultSummaryPanel title="Saved / Latest Momentum TV Results" rows={resultRows} mode="momentum" loaded={momentumTvRowsLoaded} onOpenStock={onOpenStock} />
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
  const swingTvDebugFields = ["tv_status", "tv_confirmed", "confidence_score", "reason", "rejection_reason", "weekly_bias", "daily_setup", "four_hour_confirmation", "one_hour_entry", "candles_1W", "candles_1D", "candles_4H", "candles_1H", "paper_plan_valid", "paper_plan_reason", "entry_readiness", "next_action_for_paper_trade", "fake_breakout_risk", "retail_trap_risk", "trade_quality_grade", "swing_explanation", "entry_comment", "error"];
  const momentumPrecheckDebugFields = ["momentum_score", "momentum_candidate", "momentum_status", "overextended", "eligible_for_tv_confirm", "score_breakdown"];
  const momentumTvDebugFields = ["tv_status", "tv_confirmed", "confidence_score", "reason", "rejection_reason", "daily_momentum", "four_hour_confirmation", "one_hour_entry", "candles_1D", "candles_4H", "candles_1H", "paper_plan_valid", "paper_plan_reason", "entry_readiness", "next_action_for_paper_trade", "fake_breakout_risk", "overextended_risk", "volume_confirmation", "entry_quality", "trap_status", "trap_reason", "momentum_trap_score", "momentum_trap_summary", "trade_quality_grade", "momentum_explanation", "entry_comment", "error"];
  const savedConfirmationFields = ["symbol", "tradingview_symbol", "tv_status", "trade_quality_grade", "reason", "updated_at"];
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

function PaperTrades({ signals, plans, activeTrades, allTrades, onSignals, onPlans, onActive, onAll, onUpdate, loading }) {
  return <div className="pageStack">
    <div className="buttonRow"><ActionButton onClick={onSignals} disabled={loading}>Load Paper Signals</ActionButton><ActionButton onClick={onPlans} disabled={loading}>Load Paper Plans</ActionButton><ActionButton onClick={onActive} disabled={loading}>Load Active Trades</ActionButton><ActionButton onClick={onAll} disabled={loading}>Load All Trades</ActionButton><ActionButton onClick={onUpdate} disabled={loading}>Update Paper Trades</ActionButton></div>
    <div className="paperTabs"><Badge tone="green">Signals</Badge><Badge tone="yellow">Plans</Badge><Badge tone="green">Active</Badge><Badge tone="red">Stopped</Badge></div>
    <div className="twoGrid"><Card title="Paper Signals"><MiniTable rows={signals} columns={["symbol", "status", "entry", "sl", "t1", "rr", "next_action"]} /></Card><Card title="Paper Plans"><MiniTable rows={plans} columns={["symbol", "source_signal_type", "status", "entry_price", "stop_loss", "target_1", "target_2", "target_3", "risk_reward_1", "next_action_for_paper_trade"]} /></Card><Card title="Active Trades"><MiniTable rows={activeTrades} columns={["symbol", "source_signal_type", "status", "paper_pnl"]} /></Card><Card title="All Trades"><MiniTable rows={allTrades} columns={["symbol", "source_signal_type", "status", "paper_pnl"]} /></Card></div>
  </div>;
}

function Settings({ settings, health }) {
  return <div className="settingsPage"><Card title="Safety Locks" eyebrow="read-only"><div className="settingsGrid"><div><span>paper_only</span><strong>true</strong></div><div><span>live_trading</span><strong>false</strong></div><div><span>broker_orders</span><strong>false</strong></div><div><span>yfinance_for_tv_candles</span><strong>false</strong></div></div></Card><Card title="API Status" eyebrow="local backend"><div className="settingsGrid"><div><span>backend</span><strong>{health.status}</strong></div><div><span>API base</span><strong>{API_BASE}</strong></div><div><span>TradingView port</span><strong>{settings?.tradingview_debug_port ?? 9222}</strong></div><div><span>data source</span><strong>TradingView Desktop CDP</strong></div></div></Card><Card title="TradingView Desktop Reminder"><p className="muted">Keep Chrome or TradingView Desktop running with debug port 9222, logged in, and a chart tab open.</p></Card></div>;
}

export default function App() {
  const [activePage, setActivePage] = useState("Dashboard");
  const [search, setSearch] = useState("");
  const [health, setHealth] = useState({ online: false, status: "checking" });
  const [settings, setSettings] = useState(null);
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
  const [aiDatasetSummary, setAiDatasetSummary] = useState(null);
  const [aiFeatureSnapshots, setAiFeatureSnapshots] = useState([]);
  const [aiOutcomePreview, setAiOutcomePreview] = useState(null);
  const [aiDatasetFilters, setAiDatasetFilters] = useState({ strategyType: "", timeframe: "" });
  const [paperUpdateProgress, setPaperUpdateProgress] = useState(null);
  const [paperUpdateRuns, setPaperUpdateRuns] = useState([]);
  const [paperUpdateLock, setPaperUpdateLock] = useState(null);
  const [paperUpdateScheduler, setPaperUpdateScheduler] = useState(null);
  const [paperUpdateDryRunResult, setPaperUpdateDryRunResult] = useState(null);
  const [paperUpdateApprovalText, setPaperUpdateApprovalText] = useState("");
  const [paperUpdateApprovalResult, setPaperUpdateApprovalResult] = useState(null);
  const [paperUpdateApprovalError, setPaperUpdateApprovalError] = useState("");
  const [signals, setSignals] = useState([]);
  const [plans, setPlans] = useState([]);
  const [activeTrades, setActiveTrades] = useState([]);
  const [allTrades, setAllTrades] = useState([]);
  const [tv, setTv] = useState({ symbol: "NSE:RELIANCE", timeframe: "1D" });
  const [tvResult, setTvResult] = useState(null);
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

  const act = async (name, fn) => {
    setLoading(name); setError(""); setNotice("");
    try { const data = await fn(); setLastResponse(data); return data; }
    catch (err) { console.error(`${name} failed`, err); setError(formatActionError(err, name)); return null; }
    finally { setLoading(""); }
  };

  const refreshPaperUpdateSafety = async () => {
    const [progress, runs, lock, scheduler] = await Promise.all([
      getPaperUpdateProgress(),
      getPaperUpdateRuns(10),
      getPaperUpdateLock(),
      getPaperUpdateSchedulerStatus(),
    ]);
    const runRows = arr(runs, ["runs"]);
    setPaperUpdateProgress(progress);
    setPaperUpdateRuns(runRows);
    setPaperUpdateLock(lock);
    setPaperUpdateScheduler(scheduler);
    return { progress, runs: runRows, lock, scheduler };
  };
  const refreshPaperDashboardStatus = async () => {
    const [safetyResult, summaryResult, equityResult] = await Promise.allSettled([
      refreshPaperUpdateSafety(),
      getPaperSummary(),
      getPaperEquity(),
    ]);
    if (summaryResult.status === "fulfilled") {
      setSummary(summaryResult.value);
    } else {
      console.error("paper summary refresh failed", summaryResult.reason);
    }
    if (safetyResult.status === "rejected") {
      console.error("paper update safety refresh failed", safetyResult.reason);
    }
    if (equityResult.status === "rejected") {
      console.error("paper equity refresh failed", equityResult.reason);
    }
    return {
      safety: safetyResult.status === "fulfilled" ? safetyResult.value : null,
      summary: summaryResult.status === "fulfilled" ? summaryResult.value : null,
      paper_equity: equityResult.status === "fulfilled" ? equityResult.value : null,
    };
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
    act("status", async () => {
      const [healthData, settingsData] = await Promise.all([getHealth(), getSettings()]);
      setHealth({ online: healthData?.status === "ok", status: healthData?.status || "unknown" });
      setSettings(settingsData);
      return { health: healthData, settings: settingsData };
    }).catch(() => setHealth({ online: false, status: "offline" }));
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getAiFeatureDatasetSummary(), getAiFeatureSnapshots(50), getAiOutcomePreview(50)])
      .then(([summaryData, snapshotsData, outcomePreviewData]) => {
        if (!cancelled) {
          setAiDatasetSummary(summaryData);
          setAiFeatureSnapshots(arr(snapshotsData, ["rows"]));
          setAiOutcomePreview(outcomePreviewData);
        }
      })
      .catch((err) => console.error("AI dataset tracking load failed", err));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      getPaperUpdateProgress(),
      getPaperUpdateRuns(10),
      getPaperUpdateLock(),
      getPaperUpdateSchedulerStatus(),
    ])
      .then(([progress, runs, lock, scheduler]) => {
        if (cancelled) return;
        setPaperUpdateProgress(progress);
        setPaperUpdateRuns(arr(runs, ["runs"]));
        setPaperUpdateLock(lock);
        setPaperUpdateScheduler(scheduler);
      })
      .catch((err) => console.error("paper update safety load failed", err));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const searched = normalizeSearchSymbol(search);
    setStockSwingPrecheck(null);
    setStockMomentumPrecheck(null);
    setStockSwingTvResult(null);
    setStockMomentumTvResult(null);
    setStockSavedSwingResult(null);
    setStockSavedMomentumResult(null);
    if (activePage !== "Stock Detail" || !searched.symbol) {
      setStockMarketData(null);
      return undefined;
    }
    const timerId = window.setTimeout(() => {
      Promise.all([
        getMarketDataSymbol({ exchange: searched.exchange, symbol: searched.symbol }),
        getSwingPrecheck({ exchange: searched.exchange, symbol: searched.symbol }),
        getMomentumPrecheck({ exchange: searched.exchange, symbol: searched.symbol }),
        getSwingTvConfirmed({ limit: 200 }),
        getMomentumTvConfirmed({ limit: 200 }),
      ])
        .then(([marketData, swingPrecheck, momentumPrecheck, savedSwing, savedMomentum]) => {
          setStockMarketData(marketData);
          setStockSwingPrecheck(swingPrecheck);
          setStockMomentumPrecheck(momentumPrecheck);
          setStockSavedSwingResult(savedSwing);
          setStockSavedMomentumResult(savedMomentum);
        })
        .catch((err) => {
          console.error("stock detail load failed", err);
          setStockMarketData({ found: false, error: err.message || String(err) });
        });
    }, 350);
    return () => window.clearTimeout(timerId);
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
      const [summaryData, snapshotsData, outcomePreviewData] = await Promise.all([
        getAiFeatureDatasetSummary(aiDatasetFilters),
        getAiFeatureSnapshots(50),
        getAiOutcomePreview(50),
      ]);
      setAiDatasetSummary(summaryData);
      setAiFeatureSnapshots(arr(snapshotsData, ["rows"]));
      setAiOutcomePreview(outcomePreviewData);
      return { summary: summaryData, snapshots: snapshotsData, outcome_preview: outcomePreviewData };
    }),
    paperUpdateDryRun: () => {
      if (!window.confirm(PAPER_UPDATE_DRY_RUN_CONFIRM)) {
        setNotice("Paper update dry-run cancelled.");
        return null;
      }
      return act("paper update dry-run", async () => {
        setPaperUpdateApprovalText("");
        setPaperUpdateApprovalResult(null);
        setPaperUpdateApprovalError("");
        const result = await runPaperUpdateDryRun({ maxTrades: 6, maxWrites: 1 });
        const refreshed = await refreshPaperDashboardStatus();
        const normalizedResult = normalizeDryRunForApproval(
          result,
          refreshed.safety?.progress,
          refreshed.safety?.runs,
        );
        setPaperUpdateDryRunResult(normalizedResult);
        return { dry_run_result: normalizedResult, refreshed };
      });
    },
    paperUpdateApprove: () => {
      const approvalDryRun = normalizeDryRunForApproval(
        paperUpdateDryRunResult,
        paperUpdateProgress,
        paperUpdateRuns,
      );
      const gate = canApprovePaperRealUpdate({
        latestDryRun: approvalDryRun,
        lockStatus: paperUpdateLock,
        schedulerStatus: paperUpdateScheduler,
      });
      if (!gate.allowed) {
        const reasonText = gate.reasons.length ? gate.reasons.join("\n") : "Unknown gate failure.";
        setPaperUpdateApprovalError(`Approval gate failed. Backend was not called.\n${reasonText}`);
        setNotice("Paper approval blocked by the local gate. Run a fresh dry-run when ready.");
        return null;
      }
      if (paperUpdateApprovalText !== PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT) {
        setPaperUpdateApprovalError("Exact confirmation text is required. Backend was not called.");
        setNotice("Paper approval blocked until the exact confirmation text is typed.");
        return null;
      }
      return act("paper update approval", async () => {
        let response = null;
        try {
          response = await approvePaperUpdateFromDryRun({
            approvedDryRunId: approvalDryRun.run_id,
            confirmationText: PAPER_UPDATE_APPROVAL_CONFIRMATION_TEXT,
            maxTrades: PAPER_UPDATE_APPROVAL_MAX_TRADES,
            maxWrites: PAPER_UPDATE_APPROVAL_MAX_WRITES,
          });
          setPaperUpdateApprovalResult(response);
          if (response?.blocked) {
            setPaperUpdateApprovalError(`Approval rejected: ${paperUpdateApprovalRejectionReason(response)}. Run a fresh dry-run before trying approval again.`);
          } else {
            setPaperUpdateApprovalError("");
          }
          return { approval_result: response };
        } catch (err) {
          const rejectionReason = paperUpdateApprovalRejectionReason(err?.responseBody);
          setPaperUpdateApprovalError(`Approval request failed${rejectionReason !== "-" ? `: ${rejectionReason}` : ""}. Run a fresh dry-run before trying approval again.`);
          throw err;
        } finally {
          setPaperUpdateDryRunResult(null);
          setPaperUpdateApprovalText("");
          await refreshPaperDashboardStatus();
        }
      });
    },
    dryRun: () => act("pipeline dry run", () => runPaperPipeline({ limit: 1, timeframe: "1D", dryRun: true, strategy: "swing" })),
    saveRun: () => act("pipeline save", () => runPaperPipeline({ limit: 1, timeframe: "1D", dryRun: false, strategy: "swing" })),
    scan: () => act("scan", async () => { const data = await runScan(); if (data?.scan_run_id) setLatestScanRunId(data.scan_run_id); setScanRows(await getPaperSafeScanRows(data?.scan_run_id)); return data; }),
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
    swing: () => act("swing candidates", async () => { const summaryData = await getSwingSummary(); const limit = positiveCount(summaryData?.swing_candidates_count); const data = await getSwingCandidates("BROAD_MARKET_750", limit); setSwingRows(arr(data, ["candidates", "rows"])); setSwingSummary(summaryData); setSwingTvLimit(limit); setSwingCandidatesStale(Boolean(summaryData?.is_score_stale)); return data; }),
    swingTvConfirm: () => act("swing batch tv confirm", async () => {
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
          const batch = await swingTvConfirm({ limit: currentLimit, offset, batchNumber, batchSize, timeframes: SWING_MTF_TIMEFRAMES, save: true });
          const rows = tvRows(batch);
          processedSoFar += Number(batch?.processed || 0);
          completed.push(batch);
          setSwingBatchResults([...completed]);
          setLatestSwingTvRows((current) => [...current, ...rows]);
          setSwingTvRowsLoaded(true);
          setSwingBatchProgress((current) => current ? { ...current, processed_so_far: processedSoFar, elapsed_time: Math.floor((Date.now() - startedAt) / 1000) } : current);
        } catch (err) {
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
      return { total_requested: totalRequested, batch_size: batchSize, batches_completed: completed.length, results: completed };
    }),
    swingSavedTv: () => act("saved swing tv results", async () => {
      const data = await getSwingTvConfirmed({ limit: 50 });
      setSwingSavedTvResult(data);
      setLatestSwingTvRows(tvRows(data));
      setSwingTvRowsLoaded(true);
      return data;
    }),
    swingStopBatch: () => { swingBatchStopRef.current = true; setSwingBatchStopRequested(true); },
    swingSignals: () => act("swing signals", () => buildSwingSignals(false)),
    swingPlans: () => act("swing plans", () => buildPaperPlans("SWING_TV_CONFIRMED", false)),
    momentumSummary: () => act("momentum summary", async () => { const data = await getMomentumSummary(); const limit = positiveCount(data?.momentum_candidates_count || 10); setMomentumSummary(data); setMomentumTvLimit(limit); setMomentumBatchTotal(limit); setMomentumCandidatesStale(Boolean(data?.is_score_stale)); return data; }),
    momentum: () => act("momentum candidates", async () => { const summaryData = await getMomentumSummary(); const limit = positiveCount(summaryData?.momentum_candidates_count); const data = await getMomentumCandidates("BROAD_MARKET_750", limit); setMomentumRows(arr(data, ["candidates", "rows"])); setMomentumSummary(summaryData); setMomentumTvLimit(limit); setMomentumBatchTotal(limit); setMomentumCandidatesStale(Boolean(summaryData?.is_score_stale)); return data; }),
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
      const data = await getMomentumTvConfirmed({ limit: 20 });
      const normalized = normalizeSavedTvResponse(data, 20);
      setMomentumSavedTvResult(normalized);
      setLatestMomentumTvRows(tvRows(normalized));
      setMomentumTvRowsLoaded(true);
      return normalized;
    }),
    momentumBatchConfirm: () => act("momentum batch tv confirm", async () => {
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
          const batch = await momentumTvConfirm({ limit: currentLimit, offset, batchNumber, batchSize, timeframes: MOMENTUM_MTF_TIMEFRAMES, save: true, forceUseStaleScores: false });
          const rows = tvRows(batch);
          processedSoFar += Number(batch?.processed || 0);
          completed.push(batch);
          setMomentumBatchResults([...completed]);
          setLatestMomentumTvRows((current) => [...current, ...rows]);
          setMomentumTvRowsLoaded(true);
          setMomentumBatchProgress((current) => current ? { ...current, processed_so_far: processedSoFar, elapsed_time: Math.floor((Date.now() - startedAt) / 1000) } : current);
        } catch (err) {
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
      return { total_requested: totalRequested, batch_size: batchSize, batches_completed: completed.length, results: completed };
    }),
    momentumStopBatch: () => { momentumBatchStopRef.current = true; setMomentumBatchStopRequested(true); },
    momentumSignals: () => act("momentum signals", () => buildMomentumSignals(false)),
    momentumPlans: () => act("momentum plans", () => buildPaperPlans("MOMENTUM_TV_CONFIRMED", false)),
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
      const data = await swingTvConfirm({ limit: 1, timeframes: stockSwingTimeframes, save: false, singleSymbol: true, symbol: searched.symbol, tradingviewSymbol: searched.tradingview_symbol, exchange: searched.exchange });
      setStockSwingTvResult(data);
      return data;
    }),
    stockMomentumTvConfirm: () => act("stock momentum tv confirm", async () => {
      const searched = normalizeSearchSymbol(search);
      const data = await momentumTvConfirm({ limit: 1, timeframes: stockMomentumTimeframes, save: false, forceUseStaleScores: false, singleSymbol: true, symbol: searched.symbol, tradingviewSymbol: searched.tradingview_symbol, exchange: searched.exchange });
      setStockMomentumTvResult(data);
      return data;
    }),
    tvTest: () => act("tv candles", async () => { const data = await testTvSymbol(tv.symbol, tv.timeframe); setTvResult(data); return data; }),
    dryRun750: () => act("dry run 750 scan", async () => { const data = await loadAllMarketData(true); setMarketLoadResult(data); const progress = await getMarketLoadProgress(); setMarketProgress(progress); return { load_all: data, progress }; }),
    scanAll750: () => act("scan all 750 stocks", async () => { const data = await loadAllMarketData(false); setMarketLoadResult(data); const progress = await getMarketLoadProgress(); setMarketProgress(progress); setSwingCandidatesStale(true); setMomentumCandidatesStale(true); setMarketDataNeedsScore(true); setNotice(SCAN_SCORE_WARNING); return { load_all: data, progress }; }),
    paperSignals: () => act("paper signals", async () => { const data = await getPaperSignals(); setSignals(arr(data, ["signals"])); return data; }),
    paperPlans: () => act("paper plans", async () => { const data = await getPaperPlans(); setPlans(arr(data, ["plans"])); return data; }),
    active: () => act("active trades", async () => { const data = await getActiveTrades(); setActiveTrades(arr(data, ["trades"])); return data; }),
    all: () => act("all trades", async () => { const data = await getAllTrades(); setAllTrades(arr(data, ["trades"])); return data; }),
    update: () => act("update paper", () => updatePaperPlans()),
  };

  const page = useMemo(() => {
    if (activePage === "Swing Trading") return <SwingTrading swingRows={swingRows} swingSummary={swingSummary} latestSwingTvRows={latestSwingTvRows} swingTvRowsLoaded={swingTvRowsLoaded} swingBatchResults={swingBatchResults} swingBatchProgress={swingBatchProgress} swingBatchError={swingBatchError} swingBatchStopRequested={swingBatchStopRequested} swingBatchStopMessage={swingBatchStopMessage} swingBatchRunning={loading === "swing batch tv confirm"} candidatesStale={swingCandidatesStale} onSummary={handlers.swingSummary} onLoad={handlers.swing} onBatchConfirm={handlers.swingTvConfirm} onStopBatch={handlers.swingStopBatch} onLoadSaved={handlers.swingSavedTv} onSignals={handlers.swingSignals} onPlans={handlers.swingPlans} onOpenStock={openStockDetail} loading={!!loading} />;
    if (activePage === "Momentum Trading") return <MomentumTrading momentumRows={momentumRows} momentumSummary={momentumSummary} latestMomentumTvRows={latestMomentumTvRows} momentumTvRowsLoaded={momentumTvRowsLoaded} momentumBatchResults={momentumBatchResults} momentumBatchProgress={momentumBatchProgress} momentumBatchError={momentumBatchError} momentumBatchStopRequested={momentumBatchStopRequested} momentumBatchStopMessage={momentumBatchStopMessage} momentumBatchRunning={loading === "momentum batch tv confirm"} candidatesStale={momentumCandidatesStale} onSummary={handlers.momentumSummary} onLoad={handlers.momentum} onBatchConfirm={handlers.momentumBatchConfirm} onStopBatch={handlers.momentumStopBatch} onLoadSaved={handlers.momentumSavedTv} onSignals={handlers.momentumSignals} onPlans={handlers.momentumPlans} onOpenStock={openStockDetail} loading={!!loading} />;
    if (activePage === "Market Data") return <MarketDataPage tv={tv} setTv={setTv} tvResult={tvResult} onTest={handlers.tvTest} onDryRun750={handlers.dryRun750} onScanAll750={handlers.scanAll750} onScoreMarketData={handlers.scoreMarketData} marketLoadResult={marketLoadResult} marketProgress={marketProgress} scoreRunResult={scoreRunResult} scoreSummary={scoreSummary} marketDataNeedsScore={marketDataNeedsScore} loading={!!loading} loadingText={loading} lastResponse={lastResponse} />;
    if (activePage === "Stock Detail") return <StockDetailPage search={search} stockMarketData={stockMarketData} stockSwingPrecheck={stockSwingPrecheck} stockMomentumPrecheck={stockMomentumPrecheck} stockSwingTvResult={stockSwingTvResult} stockMomentumTvResult={stockMomentumTvResult} stockSavedSwingResult={stockSavedSwingResult} stockSavedMomentumResult={stockSavedMomentumResult} latestSwingTvRows={latestSwingTvRows} latestMomentumTvRows={latestMomentumTvRows} stockSwingTimeframes={stockSwingTimeframes} setStockSwingTimeframes={setStockSwingTimeframes} stockMomentumTimeframes={stockMomentumTimeframes} setStockMomentumTimeframes={setStockMomentumTimeframes} onLoadStockMarket={handlers.stockMarketData} onSwingPrecheck={handlers.stockSwingPrecheck} onMomentumPrecheck={handlers.stockMomentumPrecheck} onStockSwingTvConfirm={handlers.stockSwingTvConfirm} onStockMomentumTvConfirm={handlers.stockMomentumTvConfirm} loading={!!loading} />;
    if (activePage === "Paper Trades") return <PaperTrades signals={signals} plans={plans} activeTrades={activeTrades} allTrades={allTrades} onSignals={handlers.paperSignals} onPlans={handlers.paperPlans} onActive={handlers.active} onAll={handlers.all} onUpdate={handlers.update} loading={!!loading} />;
    if (activePage === "Settings") return <Settings settings={settings} health={health} />;
    return <Dashboard summary={summary} scoreSummary={scoreSummary} swingSummary={swingSummary} momentumSummary={momentumSummary} aiDatasetSummary={aiDatasetSummary} aiFeatureSnapshots={aiFeatureSnapshots} aiOutcomePreview={aiOutcomePreview} aiDatasetFilters={aiDatasetFilters} paperUpdateProgress={paperUpdateProgress} paperUpdateRuns={paperUpdateRuns} paperUpdateLock={paperUpdateLock} paperUpdateScheduler={paperUpdateScheduler} paperUpdateDryRunResult={paperUpdateDryRunResult} paperUpdateApprovalText={paperUpdateApprovalText} paperUpdateApprovalResult={paperUpdateApprovalResult} paperUpdateApprovalError={paperUpdateApprovalError} onSummary={handlers.loadSummary} onDryRun={handlers.dryRun} onSaveRun={handlers.saveRun} onAiDatasetRefresh={handlers.aiDatasetSummary} onAiDatasetFiltersChange={setAiDatasetFilters} onPaperUpdateDryRun={handlers.paperUpdateDryRun} onPaperUpdateApprove={handlers.paperUpdateApprove} onPaperUpdateApprovalTextChange={setPaperUpdateApprovalText} aiDatasetLoading={loading === "AI dataset summary"} paperUpdateDryRunLoading={loading === "paper update dry-run"} paperUpdateApprovalLoading={loading === "paper update approval"} loading={!!loading} />;
  }, [activePage, settings, health, summary, scoreSummary, swingSummary, momentumSummary, aiDatasetSummary, aiFeatureSnapshots, aiOutcomePreview, aiDatasetFilters, paperUpdateProgress, paperUpdateRuns, paperUpdateLock, paperUpdateScheduler, paperUpdateDryRunResult, paperUpdateApprovalText, paperUpdateApprovalResult, paperUpdateApprovalError, swingRows, latestSwingTvRows, swingTvRowsLoaded, swingBatchResults, swingBatchProgress, swingBatchError, swingBatchStopRequested, swingBatchStopMessage, swingCandidatesStale, momentumRows, momentumCandidatesStale, latestMomentumTvRows, momentumTvRowsLoaded, momentumBatchResults, momentumBatchProgress, momentumBatchError, momentumBatchStopRequested, momentumBatchStopMessage, stockMarketData, stockSwingPrecheck, stockMomentumPrecheck, stockSwingTvResult, stockMomentumTvResult, stockSavedSwingResult, stockSavedMomentumResult, stockSwingTimeframes, stockMomentumTimeframes, search, tv, tvResult, marketLoadResult, marketProgress, scoreRunResult, marketDataNeedsScore, signals, plans, activeTrades, allTrades, loading, lastResponse]);

  return <div className="appShell">
    <aside className="sidebar"><div className="brand"><div className="brandMark">TA</div><div><h1>Trading Agent</h1><p>Paper Terminal</p></div></div><div className="navSeparator">Workspace</div><nav>{NAV_WITH_STOCK_DETAIL.map((item) => <button className={activePage === item.label ? "navItem active" : "navItem"} key={item.label} onClick={() => setActivePage(item.label)}><span>{item.icon}</span>{item.label}</button>)}</nav><div className="sidebarFooter"><Badge tone="yellow">PAPER ONLY</Badge><p>No live trading. No broker orders.</p></div></aside>
    <div className="mainArea">
      <header className="topHeader"><div><h2>{activePage}</h2><p>API base: {API_BASE}</p></div><input className="searchInput" value={search} onChange={(e) => { setSearch(e.target.value); if (e.target.value.trim()) setActivePage("Stock Detail"); }} placeholder="Search NSE/BSE symbols..." /><div className="statusBadges"><Badge tone={health.online ? "green" : "red"}>Market API {health.status}</Badge><Badge tone="green">TradingView Desktop</Badge><Badge tone="yellow">Paper Mode</Badge></div></header>
      <section className="modeBanner"><strong>PAPER MODE / NO LIVE ORDERS</strong><span>Simulated signals and paper trade plans only.</span></section>
      {loading && <div className="noticePanel">Loading {loading}...</div>}
      {notice && <div className="warningText appNotice">{notice}</div>}
      {error && <div className="errorPanel">{error}</div>}
      <main className="pageContent">{page}</main>
    </div>
  </div>;
}
