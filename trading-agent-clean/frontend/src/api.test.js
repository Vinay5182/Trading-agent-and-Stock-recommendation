import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { approvePaperUpdateFromDryRun } from "./api.js";

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
