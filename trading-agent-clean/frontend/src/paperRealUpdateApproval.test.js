import test from "node:test";
import assert from "node:assert/strict";

import {
  PAPER_REAL_UPDATE_DRY_RUN_MAX_AGE_MS,
  canApprovePaperRealUpdate,
} from "./paperRealUpdateApproval.js";

const NOW = Date.parse("2026-06-07T12:00:00.000Z");

const safeInput = () => ({
  latestDryRun: {
    run_id: "dry-run-1",
    dry_run: true,
    status: "COMPLETED",
    mongo_writes_enabled: false,
    paper_only: true,
    live_trading: false,
    broker_orders: false,
    errors_count: 0,
    blocked: false,
    proposed_write_count: 1,
    max_writes: 1,
    finished_at: new Date(NOW - 30_000).toISOString(),
  },
  lockStatus: {
    held: false,
  },
  schedulerStatus: {
    enabled: false,
    scheduler_running: false,
    automatic_updates_enabled: false,
  },
  now: NOW,
});

const evaluate = (change = () => {}) => {
  const input = safeInput();
  change(input);
  return canApprovePaperRealUpdate(input);
};

test("denies approval when no latest dry-run exists", () => {
  const result = evaluate((input) => {
    input.latestDryRun = null;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Latest dry-run is required."));
});

test("denies approval when dry-run is older than 3 minutes", () => {
  const result = evaluate((input) => {
    input.latestDryRun.finished_at = new Date(
      NOW - PAPER_REAL_UPDATE_DRY_RUN_MAX_AGE_MS - 1,
    ).toISOString();
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run is older than 3 minutes."));
});

test("denies approval when errors_count is greater than zero", () => {
  const result = evaluate((input) => {
    input.latestDryRun.errors_count = 1;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run must have zero errors."));
});

test("denies approval when dry-run is blocked", () => {
  const result = evaluate((input) => {
    input.latestDryRun.blocked = true;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run must not be blocked."));
});

test("denies approval when proposed_write_count exceeds max_writes", () => {
  const result = evaluate((input) => {
    input.latestDryRun.proposed_write_count = 2;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run proposed_write_count exceeds max_writes."));
});

test("denies approval when max_writes does not equal one", () => {
  const result = evaluate((input) => {
    input.latestDryRun.max_writes = 2;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run max_writes must equal 1."));
});

test("denies approval when write counts are malformed", async (t) => {
  await t.test("negative proposed_write_count", () => {
    const result = evaluate((input) => {
      input.latestDryRun.proposed_write_count = -1;
    });

    assert.equal(result.allowed, false);
    assert.ok(result.reasons.includes("latestDryRun.proposed_write_count must be a non-negative integer."));
  });

  await t.test("fractional max_writes", () => {
    const result = evaluate((input) => {
      input.latestDryRun.max_writes = 0.5;
    });

    assert.equal(result.allowed, false);
    assert.ok(result.reasons.includes("latestDryRun.max_writes must be a non-negative integer."));
  });
});

test("denies approval when live trading is enabled", () => {
  const result = evaluate((input) => {
    input.latestDryRun.live_trading = true;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run must have live trading disabled."));
});

test("denies approval when broker orders are enabled", () => {
  const result = evaluate((input) => {
    input.latestDryRun.broker_orders = true;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run must have broker orders disabled."));
});

test("denies approval when paper_only is not true", () => {
  const result = evaluate((input) => {
    input.latestDryRun.paper_only = false;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run must be paper-only."));
});

test("denies approval when Mongo writes were enabled during dry-run", () => {
  const result = evaluate((input) => {
    input.latestDryRun.mongo_writes_enabled = true;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run must have Mongo writes disabled."));
});

test("denies approval when the latest run is not a dry-run", () => {
  const result = evaluate((input) => {
    input.latestDryRun.dry_run = false;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Latest run must be a dry-run."));
});

test("denies approval when the latest dry-run is not completed successfully", () => {
  const result = evaluate((input) => {
    input.latestDryRun.status = "RUNNING";
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Latest dry-run must be completed successfully."));
});

test("denies approval when any required safety field is missing", async (t) => {
  const requiredFields = [
    ["latestDryRun", "dry_run"],
    ["latestDryRun", "run_id"],
    ["latestDryRun", "status"],
    ["latestDryRun", "mongo_writes_enabled"],
    ["latestDryRun", "paper_only"],
    ["latestDryRun", "live_trading"],
    ["latestDryRun", "broker_orders"],
    ["latestDryRun", "errors_count"],
    ["latestDryRun", "blocked"],
    ["latestDryRun", "proposed_write_count"],
    ["latestDryRun", "max_writes"],
    ["latestDryRun", "finished_at"],
    ["lockStatus", "held"],
    ["schedulerStatus", "enabled"],
    ["schedulerStatus", "scheduler_running"],
    ["schedulerStatus", "automatic_updates_enabled"],
  ];

  for (const [scope, field] of requiredFields) {
    await t.test(`${scope}.${field}`, () => {
      const result = evaluate((input) => {
        delete input[scope][field];
      });

      assert.equal(result.allowed, false);
      assert.ok(result.reasons.includes(`Missing required safety field: ${scope}.${field}.`));
    });
  }
});

test("denies approval when latest dry-run ID is blank", () => {
  const result = evaluate((input) => {
    input.latestDryRun.run_id = " ";
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("latestDryRun.run_id must be a non-empty string."));
});

test("denies approval when lock is held", () => {
  const result = evaluate((input) => {
    input.lockStatus.held = true;
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Paper update lock must not be held."));
});

test("denies approval when scheduler or automatic updates are active", async (t) => {
  const schedulerFields = [
    ["enabled", "Paper update scheduler must be disabled."],
    ["scheduler_running", "Paper update scheduler must not be running."],
    ["automatic_updates_enabled", "Automatic paper updates must be disabled."],
  ];

  for (const [field, reason] of schedulerFields) {
    await t.test(field, () => {
      const result = evaluate((input) => {
        input.schedulerStatus[field] = true;
      });

      assert.equal(result.allowed, false);
      assert.ok(result.reasons.includes(reason));
    });
  }
});

test("allows approval only when every gate condition passes", () => {
  const result = evaluate();

  assert.deepEqual(result, {
    allowed: true,
    reasons: [],
  });
});

test("returns reasons explaining every failed condition", () => {
  const result = evaluate((input) => {
    input.latestDryRun.errors_count = 2;
    input.latestDryRun.blocked = true;
    input.latestDryRun.proposed_write_count = 2;
    input.lockStatus.held = true;
    input.schedulerStatus.enabled = true;
  });

  assert.equal(result.allowed, false);
  assert.deepEqual(result.reasons, [
    "Dry-run must have zero errors.",
    "Dry-run must not be blocked.",
    "Dry-run proposed_write_count exceeds max_writes.",
    "Paper update lock must not be held.",
    "Paper update scheduler must be disabled.",
  ]);
});

test("treats backend timezone-naive UTC completion timestamps as UTC", () => {
  const result = evaluate((input) => {
    input.latestDryRun.finished_at = "2026-06-07T11:59:30";
  });

  assert.equal(result.allowed, true);
  assert.deepEqual(result.reasons, []);
});

test("denies approval for a future-dated dry-run", () => {
  const result = evaluate((input) => {
    input.latestDryRun.finished_at = new Date(NOW + 1).toISOString();
  });

  assert.equal(result.allowed, false);
  assert.ok(result.reasons.includes("Dry-run completion time is in the future."));
});
