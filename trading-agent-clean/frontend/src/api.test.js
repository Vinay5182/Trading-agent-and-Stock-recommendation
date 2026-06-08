import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { approvePaperUpdateFromDryRun, getAiDataCollectionStatus, getAiFeatureDatasetSummary, getAiFeatureSnapshots, getAiOutcomePreview } from "./api.js";
import { aiDataCollectionChecklist, aiOutcomeSkippedRows, aiSnapshotDisplayRows } from "./aiDataset.js";

const CONFIRMATION_TEXT = "I understand this will write to paper_trades only and will not place broker orders";

test("approvePaperUpdateFromDryRun posts only to the bound approval endpoint", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ blocked: false, approved_dry_run_id: "dry-run-1" }),
    };
  };

  const result = await approvePaperUpdateFromDryRun({
    approvedDryRunId: "dry-run-1",
    confirmationText: CONFIRMATION_TEXT,
  });

  assert.deepEqual(result, { blocked: false, approved_dry_run_id: "dry-run-1" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://127.0.0.1:8011/api/paper/update-trades/approve");
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    approved_dry_run_id: "dry-run-1",
    confirmation_text: CONFIRMATION_TEXT,
    max_trades: 6,
    max_writes: 1,
  });
  assert.equal(calls[0].options.body.includes("dry_run=false"), false);
});

test("api source does not add an unbound dry_run=false paper update helper", () => {
  const source = readFileSync(new URL("./api.js", import.meta.url), "utf8");
  const helperStart = source.indexOf("approvePaperUpdateFromDryRun");
  const helperEnd = source.indexOf("export const getPaperUpdateProgress");
  const helperSource = source.slice(helperStart, helperEnd);

  assert.ok(helperSource.includes('"/api/paper/update-trades/approve"'));
  assert.equal(helperSource.includes("/api/paper/update-trades?"), false);
  assert.equal(source.includes("/api/paper/update-trades?dry_run=false"), false);
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
