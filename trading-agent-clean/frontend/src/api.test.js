import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { approvePaperUpdateFromDryRun, getAiFeatureDatasetSummary, getAiFeatureSnapshots } from "./api.js";
import { aiSnapshotDisplayRows } from "./aiDataset.js";

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

test("AI dataset dashboard remains read-only without training or prediction actions", () => {
  const summarySource = aiDatasetSummarySource();

  assert.ok(summarySource.includes("This dashboard is for dataset tracking only. It does not generate predictions or trade recommendations."));
  assert.ok(summarySource.includes("This table is for dataset tracking only. It does not generate predictions or trade recommendations."));
  assert.ok(summarySource.includes("AI Feature Snapshots"));
  assert.equal(/<ActionButton[^>]*>\s*(Train|Predict|Prediction|Recommend)/i.test(summarySource), false);
});
