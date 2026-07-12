import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  OPERATOR_INTENT_HEADER,
  OPERATOR_INTENT_VALUE,
  validateApiBase,
  attachTradingViewTab,
  detachTradingViewTab,
  getAiDataCollectionStatus,
  getAiFeatureDatasetSummary,
  getAiFeatureSnapshots,
  getAiOutcomePreview,
  getDashboardPaperEquity,
  getHealth,
  getMarketPipelineStatus,
  getMomentumTvConfirmed,
  getPaperHistory,
  getPaperOpenTrades,
  getPaperSummary,
  getScanRows,
  getSwingTvConfirmed,
  getSystemRuntimeInfo,
  getTradingViewRuntimeStatus,
  isRequestCancellation,
  loadAllMarketData,
  momentumTvConfirm,
  runPaperPipeline,
  runScoring,
  swingTvConfirm,
  deriveTradingViewBusy,
  tradingViewBadge,
  isBatchReady,
  canStartTradingViewOperation,
} from "./api.js";
import { aiDataCollectionChecklist, aiOutcomeSkippedRows, aiSnapshotDisplayRows } from "./aiDataset.js";
import {
  CONFIRMATION_TIME_UNAVAILABLE,
  confirmationTimestampLabel,
  confirmationTimestampValue,
  formatIstTimestamp,
} from "./timestampUtils.js";

test("frontend source does not expose manual paper update endpoints", () => {
  const source = readFileSync(new URL("./api.js", import.meta.url), "utf8");
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const combined = `${source}\n${appSource}`;
  const forbiddenPaths = [
    ["sync", "trade", "ready"],
    ["update", "trades"],
    ["update", "plans"],
    ["auto", "update", "outcomes"],
  ].map((parts) => `/api/paper/${parts.join("-")}`);

  for (const path of forbiddenPaths) {
    assert.equal(combined.includes(path), false);
  }
});

test("paper page read helpers use grouped paper APIs", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true }) };
  };

  await getPaperOpenTrades();
  await getPaperHistory();
  await getPaperSummary();

  assert.deepEqual(calls.map((call) => call.url), [
    "http://127.0.0.1:8011/api/paper/open",
    "http://127.0.0.1:8011/api/paper/history",
    "http://127.0.0.1:8011/api/paper/summary",
  ]);
  assert.equal(calls.some((call) => call.url.endsWith("/api/paper/trades")), false);
  assert.equal(calls.some((call) => call.url.endsWith("/api/paper/active")), false);
  assert.equal(calls.some((call) => call.url.endsWith(`/api/paper/${["pipeline", "details"].join("-")}`)), false);

  const source = readFileSync(new URL("./api.js", import.meta.url), "utf8");
  assert.equal(source.includes("getAllTrades"), false);
  assert.equal(source.includes("getActiveTrades"), false);
  assert.equal(source.includes(["getPaperPipeline", "Details"].join("")), false);
});

test("scan helpers match active backend routes", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true }) };
  };

  await getScanRows("scan-1");
  await getTradingViewRuntimeStatus();

  assert.equal(calls[0].url, "http://127.0.0.1:8011/api/scan/rows?scan_run_id=scan-1");
  assert.equal(calls[1].url, "http://127.0.0.1:8011/api/tv/runtime-status");
});

test("saved TV helpers request current scoped endpoints without arbitrary limits", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify({ rows: [] }) };
  };

  await getSwingTvConfirmed();
  await getMomentumTvConfirmed();
  await getSwingTvConfirmed({ limit: 5 });
  await getMomentumTvConfirmed({ limit: 6 });

  assert.equal(calls[0].url, "http://127.0.0.1:8011/api/swing/tv-confirmed?index_name=BROAD_MARKET_750");
  assert.equal(calls[1].url, "http://127.0.0.1:8011/api/momentum/tv-confirmed?index_name=BROAD_MARKET_750");
  assert.equal(calls[2].url, "http://127.0.0.1:8011/api/swing/tv-confirmed?index_name=BROAD_MARKET_750&limit=5");
  assert.equal(calls[3].url, "http://127.0.0.1:8011/api/momentum/tv-confirmed?index_name=BROAD_MARKET_750&limit=6");
});

test("Load Saved TV actions clear stale state and show current candidate scope", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.ok(appSource.includes("setSwingSavedTvResult(null);"));
  assert.ok(appSource.includes("setLatestSwingTvRows([]);"));
  assert.ok(appSource.includes("setSwingTvRowsLoaded(false);"));
  assert.ok(appSource.includes("setMomentumSavedTvResult(null);"));
  assert.ok(appSource.includes("setLatestMomentumTvRows([]);"));
  assert.ok(appSource.includes("setMomentumTvRowsLoaded(false);"));
  assert.ok(appSource.includes("getSwingTvConfirmed();"));
  assert.ok(appSource.includes("getMomentumTvConfirmed();"));
  assert.equal(appSource.includes("getSwingTvConfirmed({ limit: 50 })"), false);
  assert.equal(appSource.includes("getMomentumTvConfirmed({ limit: 20 })"), false);
  assert.ok(appSource.includes("Only {savedResultCount}/{candidateCount} current {strategyLabel} candidates have saved TV results."));
  assert.ok(appSource.includes("stale or outside-scope saved TV rows ignored."));
  assert.ok(appSource.includes("metadata={swingSavedTvResult}"));
  assert.ok(appSource.includes("metadata={momentumSavedTvResult}"));
});

test("operator intent header is scoped to trusted mutating helpers", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true }) };
  };

  await getHealth();
  await getTradingViewRuntimeStatus();
  await runScoring();
  await loadAllMarketData(true);
  await loadAllMarketData(false);
  await runPaperPipeline({ dryRun: true });
  await runPaperPipeline({ dryRun: false });
  await swingTvConfirm({ save: false });
  await swingTvConfirm({ save: true });
  await momentumTvConfirm({ save: false });
  await momentumTvConfirm({ save: true });
  await attachTradingViewTab("chart-1");
  await detachTradingViewTab();

  const operatorHeader = (index) => calls[index].options.headers[OPERATOR_INTENT_HEADER];
  assert.equal(operatorHeader(0), undefined);
  assert.equal(operatorHeader(1), undefined);
  assert.equal(operatorHeader(2), OPERATOR_INTENT_VALUE);
  assert.equal(operatorHeader(3), undefined);
  assert.equal(operatorHeader(4), OPERATOR_INTENT_VALUE);
  assert.equal(operatorHeader(5), undefined);
  assert.equal(operatorHeader(6), OPERATOR_INTENT_VALUE);
  assert.equal(operatorHeader(7), undefined);
  assert.equal(operatorHeader(8), OPERATOR_INTENT_VALUE);
  assert.equal(operatorHeader(9), undefined);
  assert.equal(operatorHeader(10), OPERATOR_INTENT_VALUE);
  assert.equal(operatorHeader(11), OPERATOR_INTENT_VALUE);
  assert.equal(operatorHeader(12), OPERATOR_INTENT_VALUE);
  assert.equal(calls[2].url, "http://127.0.0.1:8011/api/score/run?index_name=BROAD_MARKET_750&dry_run=false");
  assert.equal(calls[3].url, "http://127.0.0.1:8011/api/market/load-all?index_name=BROAD_MARKET_750&dry_run=true");
  assert.equal(calls[4].url, "http://127.0.0.1:8011/api/market/load-all?index_name=BROAD_MARKET_750&dry_run=false");
});

test("market pipeline preview and status helpers stay read-only", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true }) };
  };

  await loadAllMarketData();
  await runScoring("BROAD_MARKET_750", true);
  await getMarketPipelineStatus();

  assert.equal(calls[0].url, "http://127.0.0.1:8011/api/market/load-all?index_name=BROAD_MARKET_750&dry_run=true");
  assert.equal(calls[0].options.headers[OPERATOR_INTENT_HEADER], undefined);
  assert.equal(calls[1].url, "http://127.0.0.1:8011/api/score/run?index_name=BROAD_MARKET_750&dry_run=true");
  assert.equal(calls[1].options.headers[OPERATOR_INTENT_HEADER], undefined);
  assert.equal(calls[2].url, "http://127.0.0.1:8011/api/market/pipeline-status");
  assert.equal(calls[2].options.headers[OPERATOR_INTENT_HEADER], undefined);
});

test("operator intent wrapper preserves caller headers and fetch controls", async (t) => {
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(url, "http://127.0.0.1:8011/api/tv/attach-tab?target_id=chart-1");
    assert.equal(options.method, "POST");
    assert.equal(options.timeoutMs, undefined);
    assert.equal(options.operatorIntent, undefined);
    assert.equal(options.headers[OPERATOR_INTENT_HEADER], OPERATOR_INTENT_VALUE);
    assert.equal(options.headers["X-Caller-Trace"], "abc123");
    assert.ok(options.signal instanceof AbortSignal);
    assert.notEqual(options.signal, controller.signal);
    return { ok: true, status: 200, text: async () => JSON.stringify({ attached: true }) };
  };

  await attachTradingViewTab("chart-1", {
    timeoutMs: 5000,
    signal: controller.signal,
    headers: { "X-Caller-Trace": "abc123" },
  });
});

test("request cancellation is typed and not a display error", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => new Promise((resolve, reject) => {
    options.signal?.addEventListener("abort", () => {
      const error = new Error("The operation was aborted.");
      error.name = "AbortError";
      reject(error);
    }, { once: true });
  });

  const controller = new AbortController();
  const requestPromise = getTradingViewRuntimeStatus({ signal: controller.signal });
  controller.abort();

  await assert.rejects(requestPromise, (err) => {
    assert.equal(isRequestCancellation(err), true);
    assert.equal(err.name, "AbortError");
    assert.equal(err.message, "Request cancelled.");
    return true;
  });
  assert.equal(isRequestCancellation(new Error("Request cancelled.")), true);
});

test("dashboard paper equity helper reads the dashboard endpoint", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify({ starting_virtual_balance: 250000 }) };
  };

  const result = await getDashboardPaperEquity();

  assert.deepEqual(result, { starting_virtual_balance: 250000 });
  assert.equal(calls[0].url, "http://127.0.0.1:8011/api/dashboard/paper-equity");
  assert.equal(calls[0].options.method, undefined);
});

test("system runtime helper reads the backend identity endpoint", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  const runtime = {
    project_root: "C:\\Users\\Asus\\OneDrive\\Documents\\Trading_Strategy\\trading-agent-clean",
    backend_pid: 1234,
    git_commit: "abcdef",
    started_at: "2026-06-21T00:00:00+00:00",
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify(runtime) };
  };

  const result = await getSystemRuntimeInfo();

  assert.deepEqual(result, runtime);
  assert.equal(calls[0].url, "http://127.0.0.1:8011/api/system/runtime-info");
  assert.equal(calls[0].options.method, undefined);
});

test("obsolete reset-build-trade-ready frontend call is removed", () => {
  const source = readFileSync(new URL("./api.js", import.meta.url), "utf8");
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.equal(source.includes("resetBuildPaperFromTradeReady"), false);
  assert.equal(source.includes("/api/paper/reset-build-trade-ready"), false);
  assert.equal(appSource.includes("resetBuildPaperFromTradeReady"), false);
  assert.equal(appSource.includes("reset-build-trade-ready"), false);
});

test("Paper Trades is live without manual load or update buttons", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const start = source.indexOf("function PaperTrades");
  const end = source.indexOf("function Settings", start);
  const paperTradesSource = source.slice(start, end);

  assert.ok(source.includes("const PAPER_TRADES_REFRESH_MS = 60000;"));
  assert.ok(source.includes("}, PAPER_TRADES_REFRESH_MS);"));
  assert.ok(source.includes("paperLiveCycleRef.current"));
  assert.ok(source.includes("function paperStrategyText"));
  assert.ok(source.includes("PAPER_TABLE_COLUMNS"));
  assert.ok(source.includes("PAPER_TRADE_FILTERS"));
  assert.ok(source.includes("PAPER_WAITING_STATUSES"));
  assert.ok(source.includes("PAPER_EXPIRED_STATUSES"));
  assert.ok(source.includes("Expired / Not Triggered"));
  assert.ok(source.includes("expired_not_triggered"));
  assert.equal(source.includes("\"CLOSED\", \"TARGET_HIT\""), false);
  assert.ok(paperTradesSource.includes("paperSearch"));
  assert.ok(paperTradesSource.includes("strategyFilter"));
  assert.ok(source.includes("Waiting for Entry"));
  assert.ok(source.includes("Active Trades"));
  assert.ok(source.includes("Completed / Stopped"));
  assert.ok(source.includes("All Trades"));
  assert.ok(paperTradesSource.includes("PaperTradeTable rows={filteredRows}"));
  assert.ok(source.includes("Loading paper trades..."));
  assert.ok(paperTradesSource.includes("No paper trades match the current filters."));
  assert.equal(paperTradesSource.includes(["Pipeline", "Details"].join(" ")), false);
  assert.equal(paperTradesSource.includes(["Paper", "Signals"].join(" ")), false);
  assert.equal(paperTradesSource.includes(["Paper", "Plans"].join(" ")), false);
  assert.equal(paperTradesSource.includes("Open Trades"), false);
  assert.equal(paperTradesSource.includes("Trade History"), false);
  assert.equal(paperTradesSource.includes("getAllTrades"), false);
  assert.equal(paperTradesSource.includes("getActiveTrades"), false);
  assert.equal(paperTradesSource.includes(["Load Paper", "Signals"].join(" ")), false);
  assert.equal(paperTradesSource.includes(["Update Paper", "Trades"].join(" ")), false);
});

test("Paper Trades P&L calendar includes ambiguous realized trades without date gating", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const calendarGroupsStart = source.indexOf("const PNL_CALENDAR_REALIZED_GROUPS");
  const calendarGroupsSource = source.slice(calendarGroupsStart, calendarGroupsStart + 160);
  const dailyMapStart = source.indexOf("function buildDailyPnlMap");
  const dailyMapEnd = source.indexOf("function buildMonthlyPnlSummary", dailyMapStart);
  const dailyMapSource = source.slice(dailyMapStart, dailyMapEnd);
  const paperStart = source.indexOf("function PaperTrades");
  const paperEnd = source.indexOf("function Settings", paperStart);
  const paperTradesSource = source.slice(paperStart, paperEnd);

  assert.ok(calendarGroupsSource.includes("\"ambiguous\""));
  assert.ok(dailyMapSource.includes("isRealizedPnlTrade(trade)"));
  assert.ok(dailyMapSource.includes("day.ambiguous += 1"));
  assert.equal(dailyMapSource.includes("Date.now()"), false);
  assert.equal(dailyMapSource.toLowerCase().includes("today"), false);
  assert.ok(paperTradesSource.includes("const ambiguousTrades = withPaperGroup(arr(history, [\"ambiguous\"]), \"ambiguous\");"));
  assert.ok(paperTradesSource.includes("const calendarTrades = dedupePaperTrades([...completedTrades, ...stoppedTrades, ...ambiguousTrades]);"));
});

test("stock detail requests cancel stale responses and abort on unmount", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.ok(source.includes("stockDetailRequestRef"));
  assert.ok(source.includes("stockDetailAbortRef.current?.abort()"));
  assert.ok(source.includes("controller.signal.aborted || stockDetailRequestRef.current !== requestId"));
  assert.ok(source.includes("return () => {"));
  assert.ok(source.includes("controller.abort();"));
});

test("polling requests have overlap guards and abort cleanup", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.ok(source.includes("dashboardRefreshCycleRef.current"));
  assert.ok(source.includes("dashboardRefreshAbortRef.current?.abort()"));
  assert.ok(source.includes("globalStatusCycleRef.current"));
  assert.ok(source.includes("globalStatusAbortRef.current?.abort()"));
  assert.ok(source.includes("paperLiveCycleRef.current"));
  assert.ok(source.includes("paperSafetyCycleRef.current"));
  assert.ok(source.includes("paperLiveAbortRef.current?.abort()"));
  assert.ok(source.includes("paperSafetyAbortRef.current?.abort()"));
  assert.ok(source.includes("tvPollAbortRef.current?.abort()"));
  assert.ok(source.includes("tvActionAbortRef.current?.abort()"));
  assert.ok(source.includes("DASHBOARD_REFRESH_MS"));
  assert.ok(source.includes("BATCH_TV_RUNTIME_REFRESH_MS"));
  assert.ok(source.includes("GLOBAL_HEALTH_REFRESH_MS"));
});

test("dashboard renders virtual balance portfolio without manual refresh controls", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const portfolioStart = source.indexOf("function DashboardPortfolio");
  const dashboardEnd = source.indexOf("function SummaryCards", portfolioStart);
  const dashboardSource = source.slice(portfolioStart, dashboardEnd);

  assert.ok(source.includes("getDashboardPaperEquity"));
  assert.ok(source.includes("refreshDashboardSnapshot"));
  assert.ok(dashboardSource.includes("Starting Virtual Capital"));
  assert.ok(dashboardSource.includes("Settled Balance"));
  assert.ok(dashboardSource.includes("Reserved Margin"));
  assert.ok(dashboardSource.includes("Available Cash"));
  assert.ok(dashboardSource.includes("Unrealized P&L"));
  assert.ok(dashboardSource.includes("Total Equity"));
  assert.ok(dashboardSource.includes("Total Realized P&L"));
  assert.ok(dashboardSource.includes("Effective Open Exposure"));
  assert.ok(dashboardSource.includes("Broker Funded Exposure"));
  assert.ok(dashboardSource.includes("Active/Partial Trade Count"));
  assert.ok(dashboardSource.includes("Total Margin Released"));
  assert.ok(dashboardSource.includes("Capital Returned From Latest Exits"));
  assert.ok(dashboardSource.includes("Drawdown"));
  assert.ok(dashboardSource.includes("Profit Factor"));
  assert.ok(dashboardSource.includes("Average RR"));
  assert.ok(dashboardSource.includes("Open-Position Exposure"));
  assert.ok(dashboardSource.includes("Swing vs Momentum"));
  assert.ok(dashboardSource.includes("Recent Completed Trades"));
  assert.ok(source.includes("Scheduler and TradingView Health"));
  assert.equal(dashboardSource.includes('?? "--"'), false);
  assert.equal(source.includes('statusCounts.waiting ?? "--"'), false);
  assert.equal(source.includes('summary?.total_trades ?? "--"'), false);
  assert.equal(dashboardSource.includes("Refresh Portfolio"), false);
});

test("page-specific status reads are scoped to their pages", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.ok(source.includes('if (activePage !== "Dashboard") return undefined;'));
  assert.ok(source.includes('if (activePage !== "Paper Trades") return undefined;'));
  assert.ok(source.includes('if (activePage !== "Stock Detail" || !searched.symbol)'));
  assert.ok(source.includes("!isRequestCancellation(err)"));
  assert.equal(source.includes('act("status"'), false);
});

test("header displays backend TradingView runtime health", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.ok(source.includes("tradingViewBadge(tvRuntimeStatus)"));
  assert.ok(source.includes("getTradingViewRuntimeStatus"));
  assert.equal(source.includes('<Badge tone="green">TradingView Desktop</Badge>'), false);
});

test("settings renders backend runtime identity", () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const settingsStart = source.indexOf("function Settings");
  const settingsEnd = source.indexOf("export default function App", settingsStart);
  const settingsSource = source.slice(settingsStart, settingsEnd);

  assert.ok(source.includes("getSystemRuntimeInfo"));
  assert.ok(source.includes("systemRuntimeInfo"));
  assert.ok(source.includes('activePage !== "Settings"'));
  assert.ok(settingsSource.includes("Running Project Identity"));
  assert.ok(settingsSource.includes("project_root"));
  assert.ok(settingsSource.includes("backend_pid"));
  assert.ok(settingsSource.includes("git_commit"));
  assert.ok(settingsSource.includes("started_at"));
});

test("getAiFeatureDatasetSummary uses the read-only summary endpoint and optional filters", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  const readinessSummary = {
    read_only: true,
    mongo_writes_enabled: false,
    ready_for_model_training: false,
    readiness_reason: ["labeled_count must be at least 100"],
    minimum_labels_for_training: 100,
    labeled_count: 1,
    unlabeled_count: 4,
    win_count: 0,
    loss_count: 1,
    breakeven_count: 0,
    missing_source_mode_count: 4,
    missing_data_completeness_count: 4,
    source_mode_distribution: { paper_trades_backfill: 1 },
    data_completeness_distribution: { minimal: 1 },
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify(readinessSummary),
    };
  };

  const result = await getAiFeatureDatasetSummary();
  await getAiFeatureDatasetSummary({ strategyType: "momentum", timeframe: "1D" });

  assert.deepEqual(result, readinessSummary);
  assert.equal(result.ready_for_model_training, false);
  assert.deepEqual(result.readiness_reason, ["labeled_count must be at least 100"]);
  assert.equal(result.missing_source_mode_count, 4);
  assert.equal(result.missing_data_completeness_count, 4);
  assert.equal(calls[0].url, "http://127.0.0.1:8011/api/ai/features/summary");
  assert.equal(calls[0].options.method, undefined);
  assert.equal(calls[1].url, "http://127.0.0.1:8011/api/ai/features/summary?strategy_type=momentum&timeframe=1D");
  assert.equal(calls[1].options.method, undefined);
});

test("getAiFeatureSnapshots parses the read-only snapshot table", async (t) => {
  const originalFetch = globalThis.fetch;
  const payload = {
    read_only: true,
    mongo_writes_enabled: false,
    rows: [
      {
        symbol: "ATHERENERG",
        result_label: "LOSS",
        linked_paper_trade_status: "SL_HIT",
      },
    ],
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(url, "http://127.0.0.1:8011/api/ai/features/snapshots?limit=50");
    assert.equal(options.method, undefined);
    return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
  };

  assert.deepEqual(await getAiFeatureSnapshots(50), payload);
});

test("getAiOutcomePreview parses the read-only dry-run preview", async (t) => {
  const originalFetch = globalThis.fetch;
  const payload = {
    read_only: true,
    dry_run: true,
    mongo_writes_enabled: false,
    eligible_attach_count: 0,
    skipped_open_count: 4,
    skipped_already_labeled_count: 1,
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(url, "http://127.0.0.1:8011/api/ai/features/outcome-preview?limit=50");
    assert.equal(options.method, undefined);
    return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
  };

  assert.deepEqual(await getAiOutcomePreview(50), payload);
});

test("getAiDataCollectionStatus parses the read-only paper data report", async (t) => {
  const originalFetch = globalThis.fetch;
  const payload = {
    read_only: true,
    mongo_writes_enabled: false,
    total_paper_trades: 6,
    terminal_trades_without_ai_snapshot_count: 0,
    labeled_ai_snapshots: 1,
    unlabeled_ai_snapshots: 4,
    outcome_attach_eligible_count: 0,
    labels_remaining_before_training: 99,
    ai_model_training_blocked: true,
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(url, "http://127.0.0.1:8011/api/ai/features/collection-status");
    assert.equal(options.method, undefined);
    return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
  };

  assert.deepEqual(await getAiDataCollectionStatus(), payload);
});

test("outcome preview rows render ATHERENERG labeled and four open skips", () => {
  const rows = aiOutcomeSkippedRows([
    { symbol: "ATHERENERG", reason: "already_labeled", result_label: "LOSS" },
    ...["IGIL", "DIACABS", "YESBANK", "IDFCFIRSTB"].map((symbol) => ({
      symbol,
      reason: "open_paper_trade",
      linked_paper_trade_status: "NOT_TRIGGERED",
    })),
  ]);

  assert.deepEqual(rows[0], {
    Symbol: "ATHERENERG",
    Reason: "already_labeled",
    "Trade status": "-",
    Label: "LOSS",
  });
  assert.equal(rows.filter((row) => row.Reason === "open_paper_trade").length, 4);
});

test("AI snapshot display rows render labeled and unlabeled linked trades", () => {
  const rows = aiSnapshotDisplayRows([
    {
      symbol: "ATHERENERG",
      strategy_type: "swing",
      timeframe: "1D",
      source_mode: "paper_trades_backfill",
      data_completeness: "minimal",
      result_label: "LOSS",
      linked_paper_trade_status: "SL_HIT",
      outcome_attached_at: "2026-01-05T15:30:00",
    },
    {
      symbol: "IGIL",
      strategy_type: "momentum",
      timeframe: "1D",
      result_label: null,
      linked_paper_trade_status: "NOT_TRIGGERED",
      outcome_attached_at: null,
    },
  ]);

  assert.deepEqual(rows[0], {
    Symbol: "ATHERENERG",
    Strategy: "swing / 1D",
    Source: "paper_trades_backfill",
    Completeness: "minimal",
    Label: "LOSS",
    "Trade status": "SL_HIT",
    "Outcome attached?": "yes",
  });
  assert.equal(rows[1].Symbol, "IGIL");
  assert.equal(rows[1].Label, "unlabeled");
  assert.equal(rows[1]["Trade status"], "NOT_TRIGGERED");
  assert.equal(rows[1]["Outcome attached?"], "no");
});

test("AI data collection checklist calculates labels remaining and readiness inputs", () => {
  const checklist = aiDataCollectionChecklist({
    minimum_labels_for_training: 100,
    labeled_count: 1,
    unlabeled_count: 4,
    win_count: 0,
    loss_count: 1,
    breakeven_count: 0,
    missing_source_mode_count: 4,
    missing_data_completeness_count: 4,
    ready_for_model_training: false,
  }, {
    eligible_attach_count: 0,
  });

  assert.equal(checklist.labelsRemaining, 99);
  assert.equal(checklist.hasTwoLabelClasses, false);
  assert.equal(checklist.unlabeledCount, 4);
  assert.equal(checklist.missingMetadataCount, 8);
  assert.equal(checklist.readyForModelTraining, false);
});

const aiDatasetSummarySource = () => {
  const source = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const summaryStart = source.indexOf("function AiDatasetSummary");
  const summaryEnd = source.indexOf("function Dashboard", summaryStart);
  return source.slice(summaryStart, summaryEnd);
};

test("dashboard renders ready_for_model_training false as not ready", () => {
  const summarySource = aiDatasetSummarySource();

  assert.ok(summarySource.includes("AI Dataset Summary"));
  assert.ok(summarySource.includes("ready_for_model_training"));
  assert.ok(summarySource.includes("AI model training is not ready yet."));
  assert.ok(summarySource.includes('readyForModelTraining ? "YES" : "NO"'));
});

test("dashboard renders readiness reasons and missing metadata counts", () => {
  const summarySource = aiDatasetSummarySource();

  assert.ok(summarySource.includes("readiness_reason"));
  assert.ok(summarySource.includes('<ul className="readinessReasonList">'));
  assert.ok(summarySource.includes("missing_source_mode_count"));
  assert.ok(summarySource.includes("missing_data_completeness_count"));
  assert.ok(summarySource.includes("source_mode_distribution"));
  assert.ok(summarySource.includes("data_completeness_distribution"));
});

test("dashboard renders the read-only AI data collection checklist and not-ready messages", () => {
  const summarySource = aiDatasetSummarySource();

  assert.ok(summarySource.includes("AI Data Collection Checklist"));
  assert.ok(summarySource.includes("Labels Remaining"));
  assert.ok(summarySource.includes("At Least Two Label Classes"));
  assert.ok(summarySource.includes("Keep collecting paper trades."));
  assert.ok(summarySource.includes("Attach outcomes only after trades are closed."));
  assert.ok(summarySource.includes("Do not train a model until readiness becomes true."));
  assert.ok(summarySource.includes("This is dataset tracking only, not trade advice."));
});

test("dashboard renders the read-only paper data collection status report", () => {
  const summarySource = aiDatasetSummarySource();

  assert.ok(summarySource.includes("Paper Data Collection Status"));
  assert.ok(summarySource.includes("Terminal Missing AI Snapshot"));
  assert.ok(summarySource.includes("Outcome Attach Eligible"));
  assert.ok(summarySource.includes("Labels Remaining Before Training"));
  assert.ok(summarySource.includes("AI model training is still blocked."));
});

test("AI dataset dashboard remains read-only without training or prediction actions", () => {
  const summarySource = aiDatasetSummarySource();

  assert.ok(summarySource.includes("This dashboard is for dataset tracking only. It does not generate predictions or trade recommendations."));
  assert.ok(summarySource.includes("This table is for dataset tracking only. It does not generate predictions or trade recommendations."));
  assert.ok(summarySource.includes("AI Feature Snapshots"));
  assert.ok(summarySource.includes("Outcome Attach Preview"));
  assert.ok(summarySource.includes("AI Data Collection Checklist"));
  assert.ok(summarySource.includes("Paper Data Collection Status"));
  assert.ok(summarySource.includes("This is a dry-run preview only. It does not attach labels or modify MongoDB."));
  assert.equal(/<ActionButton[^>]*>\s*(Train|Predict|Prediction|Recommend)/i.test(summarySource), false);
  assert.equal(/<ActionButton[^>]*>\s*(Attach|Write|Save|Collect)/i.test(summarySource), false);
});

test("TradingView busy status derivation and badge display rules", () => {
  const idleStatus = { worker_running: false, queue_length: 0, active_operation: null, connected: true };
  assert.equal(deriveTradingViewBusy(idleStatus), false);
  assert.deepEqual(tradingViewBadge(idleStatus), { tone: "green", label: "TradingView Connected" });

  const disconnectedIdleStatus = { worker_running: false, queue_length: 0, active_operation: null, connected: false };
  assert.equal(deriveTradingViewBusy(disconnectedIdleStatus), false);
  assert.deepEqual(tradingViewBadge(disconnectedIdleStatus), { tone: "gray", label: "TradingView Idle" });

  const missingActiveOpStatus = { worker_running: false, queue_length: 0, connected: true };
  assert.equal(deriveTradingViewBusy(missingActiveOpStatus), false);

  const workerRunningStatus = { worker_running: true, queue_length: 0, active_operation: null, connected: true };
  assert.equal(deriveTradingViewBusy(workerRunningStatus), true);
  assert.deepEqual(tradingViewBadge(workerRunningStatus), { tone: "yellow", label: "TradingView Busy" });

  const queueLengthStatus = { worker_running: false, queue_length: 2, active_operation: null, connected: true };
  assert.equal(deriveTradingViewBusy(queueLengthStatus), true);

  const activeOpStatus = { worker_running: false, queue_length: 0, active_operation: "confirm_swing", connected: true };
  assert.equal(deriveTradingViewBusy(activeOpStatus), true);

  const recoveringStatus = { worker_running: true, recovering_from_timeout: true, manager_available: false, last_error: "timed out", connected: false };
  assert.equal(deriveTradingViewBusy(recoveringStatus), true);
  assert.deepEqual(tradingViewBadge(recoveringStatus), { tone: "yellow", label: "TradingView Recovering" });

  const errorStatus = { last_error: "connection lost", connected: true };
  assert.deepEqual(tradingViewBadge(errorStatus), { tone: "red", label: "TradingView Error" });
});

test("TradingView batch readiness and freshness logic", () => {
  const freshTime = Date.now() - 1000;
  const readyStatus = { preflight_ready: true };
  assert.equal(isBatchReady(readyStatus, freshTime), true);

  const notReadyStatus = { preflight_ready: false };
  assert.equal(isBatchReady(notReadyStatus, freshTime), false);

  const staleTime = Date.now() - 11000;
  assert.equal(isBatchReady(readyStatus, staleTime), false);
  assert.equal(isBatchReady(readyStatus, null), false);
});

test("TradingView single-tab pending: frontend displays neutral pending state", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  // 1. singleTabPending logic exists and checks the right conditions
  assert.ok(appSource.includes("const singleTabPending ="));
  assert.ok(appSource.includes("status.cdp_reachable &&"));
  assert.ok(appSource.includes("(status.valid_chart_target_count === 1) &&"));
  assert.ok(appSource.includes("!status.attached_target_id &&"));
  assert.ok(appSource.includes("!status.manual_attachment_required;"));

  // 2. Pending state renders a distinct neutral panel, not the red warning
  assert.ok(appSource.includes('className="tvDiagnosticPending"'));
  assert.ok(appSource.includes("TradingView Chart Available"));
  assert.ok(appSource.includes("One TradingView chart is available and will attach automatically when a TradingView operation runs."));
  assert.ok(appSource.includes("Will attach on next operation"));

  // 3. The CSS classes for pending state exist
  const cssSource = readFileSync(new URL("./App.css", import.meta.url), "utf8");
  assert.ok(cssSource.includes(".tvDiagnosticPending"));
  assert.ok(cssSource.includes(".tvDiagnosticPending h3"));
  assert.ok(cssSource.includes(".tvDiagnosticPending .pendingMsg"));
});

test("TradingView multi-tab: selection warning shown only for multiple valid tabs", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  // actionableWarning is the fallback that shows "Tab Selection / Attachment Required"
  assert.ok(appSource.includes('className="actionableWarning"'));
  assert.ok(appSource.includes("TradingView Tab Selection / Attachment Required"));

  // manual_attachment_required drives whether the instruction says "select in Settings"
  assert.ok(appSource.includes("status.manual_attachment_required"));
  assert.ok(appSource.includes("Please go to the Settings tab, select an active TradingView chart, and click Attach."));
});

test("TradingView diagnostic component order: ready > pending > warning", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  // The component must check in this order:
  // 1. preflight_ready === true => green (tvDiagnosticReady)
  // 2. singleTabPending => yellow (tvDiagnosticPending)
  // 3. fallthrough => red (actionableWarning)
  const readyIdx = appSource.indexOf('className="tvDiagnosticReady"');
  const pendingIdx = appSource.indexOf('className="tvDiagnosticPending"');
  const warningIdx = appSource.indexOf('className="actionableWarning"');

  assert.ok(readyIdx !== -1, "tvDiagnosticReady must exist");
  assert.ok(pendingIdx !== -1, "tvDiagnosticPending must exist");
  assert.ok(warningIdx !== -1, "actionableWarning must exist");
  assert.ok(readyIdx < pendingIdx, "ready check must come before pending check");
  assert.ok(pendingIdx < warningIdx, "pending check must come before warning fallthrough");
});

test("TradingView state synchronization, polling, and pre-batch check rules in App source code", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.ok(appSource.includes("tvPollRequestRef.current"));
  assert.ok(appSource.includes("tvActionRequestRef.current"));
  assert.ok(appSource.includes("requestId === tvPollRequestRef.current"));
  assert.ok(appSource.includes("requestId === tvActionRequestRef.current"));

  assert.ok(appSource.includes("const cancelled = isRequestCancellation(err) || signal.aborted;"));

  assert.ok(appSource.includes("tvPollTimeoutRef.current = window.setTimeout(poll, BATCH_TV_RUNTIME_REFRESH_MS);"));
  assert.ok(appSource.includes("poll();"));
  assert.ok(appSource.includes("window.clearTimeout(tvPollTimeoutRef.current);"));

  assert.ok(appSource.includes("fetchAndSetTvRuntimeStatusAction({ manual: true, forceAbort: true })"));

  assert.ok(appSource.includes("fetchAndSetTvAttachableTabs"));
  const tabsStart = appSource.indexOf("const fetchAndSetTvAttachableTabs =");
  const tabsEnd = appSource.indexOf("const act =", tabsStart);
  const tabsSource = appSource.slice(tabsStart, tabsEnd);
  assert.ok(tabsSource.includes("setTvAttachableTabs(tabs)"));

  assert.ok(appSource.includes("const isTradingPage = activePage === \"Swing Trading\" || activePage === \"Momentum Trading\";"));

  assert.ok(appSource.includes("tvTabsAbortRef.current?.abort()"));
  assert.ok(appSource.includes("tvActionAbortRef.current?.abort()"));
  assert.ok(appSource.includes("window.clearTimeout(tvPollTimeoutRef.current)"));

  assert.ok(appSource.includes("disabled={loading || !isBatchReady(tvRuntimeStatus, tvRuntimeLastUpdatedAt) || deriveTradingViewBusy(tvRuntimeStatus)}"));

  assert.ok(appSource.includes("className=\"tvDiagnosticReady\""));
  assert.ok(appSource.includes("TradingView Ready / Connected"));

  assert.ok(appSource.includes("tvRefreshStatus: () => act(\"refresh tv status\""));
});

test("page entry reads TV runtime status without discovering attachable tabs", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const effectStart = appSource.indexOf('if (activePage !== "Settings" && activePage !== "Swing Trading" && activePage !== "Momentum Trading") return undefined;');
  const effectEnd = appSource.indexOf("}, [activePage]);", effectStart);
  const effectSource = appSource.slice(effectStart, effectEnd);

  assert.ok(effectSource.includes("fetchAndSetTvRuntimeStatusAction"));
  assert.equal(effectSource.includes("fetchAndSetTvAttachableTabs"), false);

  const refreshStatusStart = appSource.indexOf('tvRefreshStatus: () => act("refresh tv status"');
  const refreshStatusEnd = appSource.indexOf('tvRefreshTabs: () => act("tv tabs"', refreshStatusStart);
  const refreshStatusSource = appSource.slice(refreshStatusStart, refreshStatusEnd);
  assert.ok(refreshStatusSource.includes("fetchAndSetTvRuntimeStatusAction"));
  assert.equal(refreshStatusSource.includes("fetchAndSetTvAttachableTabs"), false);
});

test("explicit Refresh TV Tabs remains the only attachable-tab discovery path", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const refreshTabsStart = appSource.indexOf('tvRefreshTabs: () => act("tv tabs"');
  const attachStart = appSource.indexOf('tvAttachTab:', refreshTabsStart);
  const refreshTabsSource = appSource.slice(refreshTabsStart, attachStart);

  assert.ok(refreshTabsSource.includes("fetchAndSetTvAttachableTabs({ manual: true })"));
  assert.ok(refreshTabsSource.includes("fetchAndSetTvRuntimeStatusAction({ manual: true })"));
});

test("AI dataset dashboard load handles partial endpoint failure without console error", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.ok(appSource.includes("const settledValue ="));
  assert.ok(appSource.includes("const settledErrors ="));
  assert.ok(appSource.includes("Promise.allSettled"));
  assert.ok(appSource.includes('endpoint: "/api/ai/features/collection-status"'));
  assert.equal(appSource.includes("AI dataset tracking load failed"), false);
  assert.ok(appSource.includes("Some dataset panels could not refresh"));
});

test("Wave 3: Candidate Navigation and Polling Stability App rules", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  // 1. Swing candidate opens Stock Detail once with normalized identity
  assert.ok(appSource.includes("function rowSearchSymbol(row)"));
  assert.ok(appSource.includes("const exchange = String(row?.exchange || \"NSE\").trim().toUpperCase();"));
  assert.ok(appSource.includes("return `${exchange}:${symbol}`;"));
  assert.ok(appSource.includes("const openStockDetail = (row) => {"));
  assert.ok(appSource.includes("const nextSearch = rowSearchSymbol(row);"));

  // 2. Invalid candidates do not request APIs
  assert.ok(appSource.includes("if (!raw || raw === \"-\" || raw === \"UNDEFINED\" || raw === \"NULL\")"));
  assert.ok(appSource.includes("if (activePage !== \"Stock Detail\" || !searched.symbol)"));

  // 3. One Dashboard endpoint failure does not discard other successful results
  assert.ok(appSource.includes("const refreshDashboardSnapshot = async"));
  assert.ok(appSource.includes("await Promise.allSettled(["));
  assert.ok(appSource.includes("if (equity.status === \"fulfilled\") setDashboardEquity(equity.value);"));
  assert.ok(appSource.includes("if (paperSummary.status === \"fulfilled\") setSummary(paperSummary.value);"));
  assert.ok(appSource.includes("Some dashboard panels could not refresh"));

  // 4. Abort/unmount causes no stale state update
  assert.ok(appSource.includes("controller.abort();"));
  assert.ok(appSource.includes("dashboardRefreshAbortRef.current?.abort();"));
  assert.ok(appSource.includes("tvActionAbortRef.current?.abort();"));
  assert.ok(appSource.includes("tvPollAbortRef.current?.abort();"));
  assert.ok(appSource.includes("tvTabsAbortRef.current?.abort();"));
  assert.ok(appSource.includes("stockDetailAbortRef.current?.abort();"));
});

test("TradingView batch confirm behavior and channel separation", async (t) => {
  let loadingState = "";
  let errorState = "";
  let noticeState = "";
  let tvRuntimeStatus = null;
  let tvRuntimeLastUpdatedAt = null;
  let tvRuntimeRefreshError = null;
  let tvRuntimeLoading = false;

  const tvActionInProgressRef = { current: false };
  const tvPollRequestRef = { current: 0 };
  const tvPollAbortRef = { current: null };
  const tvActionRequestRef = { current: 0 };
  const tvActionAbortRef = { current: null };
  const tvPollTimeoutRef = { current: null };

  const fetchCalls = [];
  let shouldPreflightBeReady = true;

  const mockGetTradingViewRuntimeStatus = async (options = {}) => {
    fetchCalls.push({ type: "runtime-status", options });
    if (options.signal?.aborted) {
      const err = new Error("Request cancelled.");
      err.name = "AbortError";
      throw err;
    }
    return {
      preflight_ready: shouldPreflightBeReady,
      preflight_message: "Not ready",
      worker_running: false,
      queue_length: 0,
      active_operation: null,
      connected: true,
    };
  };

  const mockSwingTvConfirm = async (options = {}) => {
    fetchCalls.push({ type: "swing-tv-confirm", options });
    return { processed: 1 };
  };

  const mockMomentumTvConfirm = async (options = {}) => {
    fetchCalls.push({ type: "momentum-tv-confirm", options });
    return { processed: 1 };
  };

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
      const status = await mockGetTradingViewRuntimeStatus({ ...options, signal });
      if (requestId === tvPollRequestRef.current && !tvActionInProgressRef.current) {
        tvRuntimeStatus = status;
        tvRuntimeLastUpdatedAt = Date.now();
        tvRuntimeRefreshError = null;
      }
      return status;
    } catch (err) {
      const cancelled = err.name === "AbortError" || signal.aborted;
      if (requestId === tvPollRequestRef.current && !tvActionInProgressRef.current) {
        if (!cancelled) {
          tvRuntimeRefreshError = err.message || String(err);
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
    tvRuntimeLoading = true;
    try {
      const status = await mockGetTradingViewRuntimeStatus({ ...options, signal });
      if (requestId === tvActionRequestRef.current) {
        tvRuntimeStatus = status;
        tvRuntimeLastUpdatedAt = Date.now();
        tvRuntimeRefreshError = null;
        tvRuntimeLoading = false;
      }
      return status;
    } catch (err) {
      const cancelled = err.name === "AbortError" || signal.aborted;
      if (requestId === tvActionRequestRef.current) {
        tvRuntimeLoading = false;
        if (!cancelled) {
          tvRuntimeRefreshError = err.message || String(err);
        }
      }
      throw err;
    } finally {
      if (tvActionAbortRef.current === controller) {
        tvActionAbortRef.current = null;
      }
    }
  };

  const act = async (name, fn) => {
    loadingState = name;
    errorState = "";
    noticeState = "";
    try {
      const data = await fn();
      return data;
    } catch (err) {
      if (err.name === "AbortError") return null;
      errorState = err.message || String(err);
      throw err;
    } finally {
      loadingState = "";
    }
  };

  let pollCancelled = false;
  const pollLoop = async () => {
    if (pollCancelled) return;
    if (tvActionInProgressRef.current) {
      tvPollTimeoutRef.current = setTimeout(pollLoop, 10);
      return;
    }
    try {
      await fetchAndSetTvRuntimeStatusPoll();
    } catch (err) {
      // Check that cancelled poll does not set tvRuntimeRefreshError
      if (err.name === "AbortError") {
        assert.equal(tvRuntimeRefreshError, null);
      }
    } finally {
      if (!pollCancelled) {
        tvPollTimeoutRef.current = setTimeout(pollLoop, 10);
      }
    }
  };

  // Start background poll
  pollLoop();

  // Wait for a poll to run
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.ok(fetchCalls.length > 0);

  // Trigger Swing batch Action
  const swingAction = async () => {
    tvActionInProgressRef.current = true;
    if (tvPollTimeoutRef.current) {
      clearTimeout(tvPollTimeoutRef.current);
      tvPollTimeoutRef.current = null;
    }
    // Abort ongoing poll
    tvPollAbortRef.current?.abort();

    try {
      const status = await fetchAndSetTvRuntimeStatusAction({ manual: true, forceAbort: true });
      if (!status || status.preflight_ready !== true) {
        throw new Error(status?.preflight_message || "Not ready");
      }
      await mockSwingTvConfirm();
      await fetchAndSetTvRuntimeStatusAction({ manual: true });
      return { ok: true };
    } finally {
      tvActionInProgressRef.current = false;
    }
  };

  fetchCalls.length = 0;
  await act("swing batch tv confirm", swingAction);

  // Verification 1: clicking Swing Run sends one fresh runtime GET, one Swing POST, then post-batch GET
  assert.equal(fetchCalls[0].type, "runtime-status");
  assert.equal(fetchCalls[1].type, "swing-tv-confirm");
  assert.equal(fetchCalls[2].type, "runtime-status");

  // Verification 2: polling cannot cancel or supersede pre-batch validation
  // and polling pauses during batch, resumes after success
  assert.equal(tvActionInProgressRef.current, false);

  // Trigger Momentum batch Action
  const momentumAction = async () => {
    tvActionInProgressRef.current = true;
    if (tvPollTimeoutRef.current) {
      clearTimeout(tvPollTimeoutRef.current);
      tvPollTimeoutRef.current = null;
    }
    tvPollAbortRef.current?.abort();

    try {
      const status = await fetchAndSetTvRuntimeStatusAction({ manual: true, forceAbort: true });
      if (!status || status.preflight_ready !== true) {
        throw new Error(status?.preflight_message || "Not ready");
      }
      await mockMomentumTvConfirm();
      await fetchAndSetTvRuntimeStatusAction({ manual: true });
      return { ok: true };
    } finally {
      tvActionInProgressRef.current = false;
    }
  };

  fetchCalls.length = 0;
  await act("momentum batch tv confirm", momentumAction);

  // Verification 3: clicking Momentum Run sends one fresh runtime GET, one Momentum POST, then post-batch GET
  assert.equal(fetchCalls[0].type, "runtime-status");
  assert.equal(fetchCalls[1].type, "momentum-tv-confirm");
  assert.equal(fetchCalls[2].type, "runtime-status");

  // Verification 4: no POST is sent when genuinely fresh preflight response is not ready
  shouldPreflightBeReady = false;
  fetchCalls.length = 0;
  await assert.rejects(act("swing batch tv confirm", swingAction), /Not ready/);
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].type, "runtime-status"); // only preflight check, no post!

  // Cleanup polling timer
  pollCancelled = true;
  if (tvPollTimeoutRef.current) {
    clearTimeout(tvPollTimeoutRef.current);
  }
});

test("invalid configuration throws errors", () => {
  assert.equal(validateApiBase(""), "http://127.0.0.1:8011");
  assert.equal(validateApiBase("/api"), "/api");
  assert.equal(validateApiBase("http://localhost:3000"), "http://localhost:3000");
  assert.equal(validateApiBase("https://my-backend.com"), "https://my-backend.com");

  assert.throws(() => validateApiBase("ftp://foo"), /protocols are supported/);
  assert.throws(() => validateApiBase("invalid_url_without_protocol"), /Invalid API base configuration/);
});

test("oversized response body is rejected safely", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  // 1. Mock fetch with large Content-Length header
  globalThis.fetch = async () => {
    return {
      ok: true,
      headers: new Map([["Content-Length", "3000000"]]), // 3MB
      text: async () => "x".repeat(3000000)
    };
  };

  await assert.rejects(getHealth(), /limit exceeded/);

  // 2. Mock fetch with chunked reader exceeding limit
  globalThis.fetch = async () => {
    return {
      ok: true,
      headers: new Map(),
      body: {
        getReader: () => {
          let count = 0;
          return {
            read: async () => {
              count++;
              if (count > 3) return { done: true };
              return { done: false, value: new Uint8Array(1000000) }; // 1MB chunk
            },
            cancel: () => {}
          };
        }
      }
    };
  };

  await assert.rejects(getHealth(), /limit exceeded/);
});

test("error sanitization redacts paths, IPs, MongoDB URIs and raw HTML", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  assert.ok(appSource.includes("sanitizeErrorMessage"));
  assert.ok(appSource.includes("[RAW_HTML_RESPONSE]"));
  assert.ok(appSource.includes("[PATH]"));
  assert.ok(appSource.includes("[IP]"));
  assert.ok(appSource.includes("[MONGO_URI]"));
  assert.ok(appSource.includes("[REDACTED]"));
  assert.ok(appSource.includes("[TRUNCATED]"));
});

test("Settings page unmount cleans up page-scoped abort controllers", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  const settingsEffectIndex = appSource.indexOf('activePage !== "Settings" && activePage !== "Swing Trading" && activePage !== "Momentum Trading"');
  assert.ok(settingsEffectIndex !== -1);
  const chunk = appSource.slice(settingsEffectIndex, settingsEffectIndex + 800);
  assert.ok(chunk.includes("tvTabsAbortRef.current?.abort()"));
  assert.ok(chunk.includes("tvActionAbortRef.current?.abort()"));
});

test("Stock Detail row-open passes exact-row identity and normalizes exchange symbol", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  assert.ok(appSource.includes("function rowSearchSymbol(row)"));
  assert.ok(appSource.includes("const exchange = String(row?.exchange || \"NSE\").trim().toUpperCase();"));
  assert.ok(appSource.includes("return `${exchange}:${symbol}`;"));
});

test("request cancellation does not display as error", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  assert.ok(appSource.includes("if (isRequestCancellation(err)) return \"\";"));
});

test("TV operation eligibility and button conditions under various states", () => {
  // 1. Ready state enables Swing/Momentum confirmation
  const readyStatus = { preflight_ready: true, cdp_reachable: true, valid_chart_target_count: 1 };
  assert.equal(canStartTradingViewOperation(readyStatus), true);

  // 2. One-tab pending state enables Swing/Momentum confirmation
  const pendingStatus = {
    preflight_ready: false,
    cdp_reachable: true,
    valid_chart_target_count: 1,
    manual_attachment_required: false,
    preflight_code: "TV_TAB_NOT_ATTACHED",
  };
  assert.equal(canStartTradingViewOperation(pendingStatus), true);

  // 3. Zero tabs disables both
  const zeroTabsStatus = {
    preflight_ready: false,
    cdp_reachable: true,
    valid_chart_target_count: 0,
    manual_attachment_required: true,
    preflight_code: "TV_TAB_NOT_ATTACHED",
  };
  assert.equal(canStartTradingViewOperation(zeroTabsStatus), false);

  // 4. Multiple tabs disables both
  const multiTabsStatus = {
    preflight_ready: false,
    cdp_reachable: true,
    valid_chart_target_count: 2,
    manual_attachment_required: true,
    preflight_code: "TV_MULTIPLE_CHART_TABS",
  };
  assert.equal(canStartTradingViewOperation(multiTabsStatus), false);

  // 5. CDP unreachable disables both
  const cdpUnreachableStatus = {
    preflight_ready: false,
    cdp_reachable: false,
    valid_chart_target_count: 0,
    manual_attachment_required: true,
    preflight_code: "TV_CDP_UNAVAILABLE",
  };
  assert.equal(canStartTradingViewOperation(cdpUnreachableStatus), false);

  // 6. manual_attachment_required disables both
  const manualRequiredStatus = {
    preflight_ready: false,
    cdp_reachable: true,
    valid_chart_target_count: 1,
    manual_attachment_required: true,
    preflight_code: "TV_TAB_NOT_ATTACHED",
  };
  assert.equal(canStartTradingViewOperation(manualRequiredStatus), false);

  // 7. running batch (busy) disables start button
  const busyStatus = {
    preflight_ready: true,
    worker_running: true,
  };
  assert.equal(canStartTradingViewOperation(busyStatus), false);
});

test("Swing and Momentum confirmation App logic checks canStartTradingViewOperation and refreshes status", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  // 8. Checks in Swing/Momentum batch confirm calls canStartTradingViewOperation
  assert.ok(appSource.includes("!canStartTradingViewOperation(status)"));

  // 9. Successful operation (offset === 0) refreshes status
  assert.ok(appSource.includes("if (offset === 0) {"));
  assert.ok(appSource.includes("await fetchAndSetTvRuntimeStatusAction({ manual: true });"));

  // 10. Attachment failure (in catch blocks) also triggers refresh to not leave UI permanently disabled
  assert.ok(appSource.includes("Failed to refresh status after batch failure"));
});

test("Frontend renders quantities, null prices, and zero waiting P&L correctly", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  // Verify shares formatting logic contains waiting, active/partial, and completed/stopped formats
  assert.ok(appSource.includes("planned_quantity"));
  assert.ok(appSource.includes("bought_quantity"));
  assert.ok(appSource.includes("open_quantity"));
  assert.ok(appSource.includes("0 bought /"));
  assert.ok(appSource.includes("bought /"));
  assert.ok(appSource.includes("open"));

  // Verify reserved margin is displayed
  assert.ok(appSource.includes("Reserved Margin"));
  assert.ok(appSource.includes("reserved_margin"));

  // Verify current price vs exit price styling/labels are present
  assert.ok(appSource.includes("Exit:"));
  assert.ok(appSource.includes("Current:"));
  assert.ok(appSource.includes("priceCell"));
});

test("confirmation timestamp helpers format UTC into IST", () => {
  assert.equal(
    formatIstTimestamp("2026-07-01T13:10:55.376000Z"),
    "01 Jul 2026, 06:40 PM IST",
  );
});

test("confirmation timestamp helpers never fall back to created_at or source candle time", () => {
  const legacy = {
    created_at: "2026-06-11T15:53:58.632000",
    updated_at: "2026-07-01T13:10:55.376000Z",
    source_candle_at: "2026-07-01T10:15:00Z",
    calculation_timestamp: "2026-07-01T13:00:00.000000Z",
  };

  assert.equal(confirmationTimestampValue(legacy, "swing"), null);
  assert.equal(confirmationTimestampLabel(legacy, "swing"), CONFIRMATION_TIME_UNAVAILABLE);
  assert.equal(confirmationTimestampValue(legacy, "momentum"), null);
  assert.equal(confirmationTimestampLabel(legacy, "momentum"), CONFIRMATION_TIME_UNAVAILABLE);
});

test("confirmation timestamp helpers keep Swing and Momentum fields independent", () => {
  const row = {
    confirmed_at: "2026-07-01T13:09:00.000000Z",
    swing_confirmed_at: "2026-07-01T13:10:55.376000Z",
    momentum_confirmed_at: "2026-07-01T13:12:00.000000Z",
  };

  assert.equal(confirmationTimestampValue(row, "swing"), row.swing_confirmed_at);
  assert.equal(confirmationTimestampValue(row, "momentum"), row.momentum_confirmed_at);
});

test("Saved TV cards display explicit confirmation labels without created_at fallback", () => {
  const appSource = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");

  assert.ok(appSource.includes("Confirmed: {confirmationTimestampLabel(row, mode)}"));
  assert.equal(appSource.includes("Confirmed: {val(row?.created_at"), false);
  assert.equal(appSource.includes("row?.created_at || row?.confirmed_at"), false);
  assert.equal(appSource.includes("row?.source_candle_at || row?.confirmed_at"), false);
});
