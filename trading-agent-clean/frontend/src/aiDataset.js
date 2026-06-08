export const aiSnapshotDisplayRows = (rows = []) => (Array.isArray(rows) ? rows : []).map((row) => ({
  Symbol: row?.symbol || "-",
  Strategy: [row?.strategy_type, row?.timeframe].filter(Boolean).join(" / ") || "-",
  Source: row?.source_mode || "missing",
  Completeness: row?.data_completeness || "missing",
  Label: row?.result_label || "unlabeled",
  "Trade status": row?.linked_paper_trade_status || "missing",
  "Outcome attached?": row?.outcome_attached_at ? "yes" : "no",
}));

export const aiOutcomeEligibleRows = (rows = []) => (Array.isArray(rows) ? rows : []).map((row) => ({
  Symbol: row?.symbol || "-",
  "Proposed label": row?.proposed_result_label || "UNKNOWN",
  "Trade status": row?.linked_paper_trade_status || "-",
}));

export const aiOutcomeSkippedRows = (rows = []) => (Array.isArray(rows) ? rows : []).map((row) => ({
  Symbol: row?.symbol || "-",
  Reason: row?.reason || "unknown",
  "Trade status": row?.linked_paper_trade_status || "-",
  Label: row?.result_label || "unlabeled",
}));
