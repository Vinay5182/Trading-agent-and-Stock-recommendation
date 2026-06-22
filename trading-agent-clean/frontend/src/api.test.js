import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  getAiDataCollectionStatus,
  getAiFeatureDatasetSummary,
  getAiFeatureSnapshots,
  getAiOutcomePreview,
  getDashboardPaperEquity,
  getPaperHistory,
  getPaperOpenTrades,
  getPaperSummary,
  getScanRows,
  getSystemRuntimeInfo,
  getTradingViewRuntimeStatus,
  isRequestCancellation,
  runScan,
} from "./api.js";
import { aiDataCollectionChecklist, aiOutcomeSkippedRows, aiSnapshotDisplayRows } from "./aiDataset.js";

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

  await runScan();
  await getScanRows("scan-1");
  await getTradingViewRuntimeStatus();

  assert.equal(calls[0].url, "http://127.0.0.1:8011/api/scan");
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    selected_index: "DEFAULT_UNIVERSE",
    limit: 50,
    force_refresh: false,
  });
  assert.equal(calls[1].url, "http://127.0.0.1:8011/api/scan/rows?scan_run_id=scan-1");
  assert.equal(calls[2].url, "http://127.0.0.1:8011/api/tv/runtime-status");
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
  assert.ok(source.includes("tvRuntimeCycleRef.current"));
  assert.ok(source.includes("tvRuntimeAbortRef.current?.abort()"));
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
  assert.ok(dashboardSource.includes("Starting Balance"));
  assert.ok(dashboardSource.includes("Current Virtual Balance"));
  assert.ok(dashboardSource.includes("Open Margin Used"));
  assert.ok(dashboardSource.includes("Available Margin"));
  assert.ok(dashboardSource.includes("Maximum Buying Power"));
  assert.ok(dashboardSource.includes("Effective Exposure"));
  assert.ok(dashboardSource.includes("Broker Funded Amount"));
  assert.ok(dashboardSource.includes("Buying Power Usage %"));
  assert.ok(dashboardSource.includes("Realized P&L"));
  assert.ok(dashboardSource.includes("Unrealized P&L"));
  assert.ok(dashboardSource.includes("Total P&L"));
  assert.ok(dashboardSource.includes("Virtual Return %"));
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
  assert.ok(source.includes('if (activePage !== "Settings") return undefined;'));
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
