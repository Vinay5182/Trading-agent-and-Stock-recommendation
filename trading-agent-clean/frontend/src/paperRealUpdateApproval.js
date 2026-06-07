export const PAPER_REAL_UPDATE_DRY_RUN_MAX_AGE_MS = 3 * 60 * 1000;

const hasOwn = (value, field) => (
  value !== null
  && typeof value === "object"
  && Object.prototype.hasOwnProperty.call(value, field)
);

const parseTimestamp = (value) => {
  if (value instanceof Date) return value.getTime();
  if (typeof value === "number") return value;
  if (typeof value !== "string" || !value.trim()) return Number.NaN;

  const timestamp = value.trim();
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp);
  return Date.parse(hasTimezone ? timestamp : `${timestamp}Z`);
};

const missingReason = (scope, field) => `Missing required safety field: ${scope}.${field}.`;

function requireExact({ value, scope, field, expected, unsafeReason, reasons }) {
  if (!hasOwn(value, field)) {
    reasons.push(missingReason(scope, field));
    return false;
  }
  if (value[field] !== expected) {
    reasons.push(unsafeReason);
    return false;
  }
  return true;
}

function requireNonNegativeInteger({ value, scope, field, reasons }) {
  if (!hasOwn(value, field)) {
    reasons.push(missingReason(scope, field));
    return null;
  }
  if (!Number.isInteger(value[field]) || value[field] < 0) {
    reasons.push(`${scope}.${field} must be a non-negative integer.`);
    return null;
  }
  return value[field];
}

function requireNonEmptyString({ value, scope, field, reasons }) {
  if (!hasOwn(value, field)) {
    reasons.push(missingReason(scope, field));
    return false;
  }
  if (typeof value[field] !== "string" || !value[field].trim()) {
    reasons.push(`${scope}.${field} must be a non-empty string.`);
    return false;
  }
  return true;
}

export function canApprovePaperRealUpdate({
  latestDryRun,
  lockStatus,
  schedulerStatus,
  now = Date.now(),
} = {}) {
  const reasons = [];

  if (latestDryRun === null || typeof latestDryRun !== "object") {
    reasons.push("Latest dry-run is required.");
  } else {
    requireNonEmptyString({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "run_id",
      reasons,
    });
    requireExact({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "dry_run",
      expected: true,
      unsafeReason: "Latest run must be a dry-run.",
      reasons,
    });
    requireExact({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "status",
      expected: "COMPLETED",
      unsafeReason: "Latest dry-run must be completed successfully.",
      reasons,
    });
    requireExact({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "mongo_writes_enabled",
      expected: false,
      unsafeReason: "Dry-run must have Mongo writes disabled.",
      reasons,
    });
    requireExact({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "paper_only",
      expected: true,
      unsafeReason: "Dry-run must be paper-only.",
      reasons,
    });
    requireExact({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "live_trading",
      expected: false,
      unsafeReason: "Dry-run must have live trading disabled.",
      reasons,
    });
    requireExact({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "broker_orders",
      expected: false,
      unsafeReason: "Dry-run must have broker orders disabled.",
      reasons,
    });
    requireExact({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "errors_count",
      expected: 0,
      unsafeReason: "Dry-run must have zero errors.",
      reasons,
    });
    requireExact({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "blocked",
      expected: false,
      unsafeReason: "Dry-run must not be blocked.",
      reasons,
    });

    const proposedWriteCount = requireNonNegativeInteger({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "proposed_write_count",
      reasons,
    });
    const maxWrites = requireNonNegativeInteger({
      value: latestDryRun,
      scope: "latestDryRun",
      field: "max_writes",
      reasons,
    });
    if (maxWrites !== null && maxWrites !== 1) {
      reasons.push("Dry-run max_writes must equal 1.");
    }
    if (
      proposedWriteCount !== null
      && maxWrites !== null
      && proposedWriteCount > maxWrites
    ) {
      reasons.push("Dry-run proposed_write_count exceeds max_writes.");
    }

    if (!hasOwn(latestDryRun, "finished_at")) {
      reasons.push(missingReason("latestDryRun", "finished_at"));
    } else {
      const nowMs = parseTimestamp(now);
      const finishedAtMs = parseTimestamp(latestDryRun.finished_at);
      if (!Number.isFinite(nowMs)) {
        reasons.push("Current time is invalid.");
      }
      if (!Number.isFinite(finishedAtMs)) {
        reasons.push("Dry-run completion time is invalid.");
      }
      if (Number.isFinite(nowMs) && Number.isFinite(finishedAtMs)) {
        const ageMs = nowMs - finishedAtMs;
        if (ageMs < 0) {
          reasons.push("Dry-run completion time is in the future.");
        } else if (ageMs > PAPER_REAL_UPDATE_DRY_RUN_MAX_AGE_MS) {
          reasons.push("Dry-run is older than 3 minutes.");
        }
      }
    }
  }

  requireExact({
    value: lockStatus,
    scope: "lockStatus",
    field: "held",
    expected: false,
    unsafeReason: "Paper update lock must not be held.",
    reasons,
  });
  requireExact({
    value: schedulerStatus,
    scope: "schedulerStatus",
    field: "enabled",
    expected: false,
    unsafeReason: "Paper update scheduler must be disabled.",
    reasons,
  });
  requireExact({
    value: schedulerStatus,
    scope: "schedulerStatus",
    field: "scheduler_running",
    expected: false,
    unsafeReason: "Paper update scheduler must not be running.",
    reasons,
  });
  requireExact({
    value: schedulerStatus,
    scope: "schedulerStatus",
    field: "automatic_updates_enabled",
    expected: false,
    unsafeReason: "Automatic paper updates must be disabled.",
    reasons,
  });

  return {
    allowed: reasons.length === 0,
    reasons,
  };
}
