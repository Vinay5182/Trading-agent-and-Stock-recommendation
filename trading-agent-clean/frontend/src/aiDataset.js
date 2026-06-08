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

const count = (value) => {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
};

export const aiDataCollectionChecklist = (summary = {}, outcomePreview = {}) => {
  const minimumLabels = count(summary?.minimum_labels_for_training);
  const labeledCount = count(summary?.labeled_count ?? summary?.labeled_snapshots);
  const labelClassCount = [summary?.win_count, summary?.loss_count, summary?.breakeven_count]
    .filter((value) => count(value) > 0).length;
  return {
    minimumLabels,
    labeledCount,
    labelsRemaining: Math.max(minimumLabels - labeledCount, 0),
    hasTwoLabelClasses: labelClassCount >= 2,
    winCount: count(summary?.win_count),
    lossCount: count(summary?.loss_count),
    breakevenCount: count(summary?.breakeven_count),
    unlabeledCount: count(summary?.unlabeled_count ?? summary?.unlabeled_snapshots),
    eligibleAttachCount: count(outcomePreview?.eligible_attach_count),
    missingMetadataCount: count(summary?.missing_source_mode_count) + count(summary?.missing_data_completeness_count),
    readyForModelTraining: summary?.ready_for_model_training === true,
  };
};
