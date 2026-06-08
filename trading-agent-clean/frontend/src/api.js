export const API_BASE = "http://127.0.0.1:8011";

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    cache: "no-store",
    ...options,
  });
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = text ? { raw: text } : {};
  }
  if (!response.ok) {
    const message = [data?.message, data?.detail, data?.error].find((item) => typeof item === "string" && item) || `HTTP ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.responseBody = data;
    error.rawBody = text;
    throw error;
  }
  return data;
}

export const getHealth = () => request("/health");
export const getSettings = () => request("/api/settings");
export const getAiFeatureDatasetSummary = ({ strategyType, timeframe } = {}) => {
  const params = new URLSearchParams();
  if (strategyType) params.set("strategy_type", strategyType);
  if (timeframe) params.set("timeframe", timeframe);
  const query = params.toString();
  return request(`/api/ai/features/summary${query ? `?${query}` : ""}`);
};
export const getAiFeatureSnapshots = (limit = 50) => request(`/api/ai/features/snapshots?limit=${encodeURIComponent(limit)}`);
export const runScan = () => request("/api/scan", {
  method: "POST",
  body: JSON.stringify({ selected_index: "DEFAULT_UNIVERSE", limit: 50, force_refresh: false }),
});
export const getScanRows = (scanRunId) => request(`/api/scan/rows${scanRunId ? `?scan_run_id=${encodeURIComponent(scanRunId)}` : ""}`);
export const runScoring = (indexName = "BROAD_MARKET_750") => request(`/api/score/run?index_name=${encodeURIComponent(indexName)}`, { method: "POST" });
export const getScoreSummary = (indexName = "BROAD_MARKET_750") => request(`/api/score/summary?index_name=${encodeURIComponent(indexName)}`);
export const getSwingSummary = (indexName = "BROAD_MARKET_750") => request(`/api/swing/summary?index_name=${encodeURIComponent(indexName)}`);
export const getSwingCandidates = (indexName = "BROAD_MARKET_750", limit = 100) => request(`/api/swing/candidates?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}`);
export const getSwingTvConfirmed = ({ indexName = "BROAD_MARKET_750", limit } = {}) => (
  request(`/api/swing/tv-confirmed?index_name=${encodeURIComponent(indexName)}${limit ? `&limit=${encodeURIComponent(limit)}` : ""}`)
);
export const getMomentumSummary = (indexName = "BROAD_MARKET_750") => request(`/api/momentum/summary?index_name=${encodeURIComponent(indexName)}`);
export const getMomentumCandidates = (indexName = "BROAD_MARKET_750", limit = 100) => request(`/api/momentum/candidates?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}`);
export const getMarketDataSymbol = ({ exchange = "NSE", symbol }) => request(`/api/market/data/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}`);
export const getSwingPrecheck = ({ exchange = "NSE", symbol, indexName = "BROAD_MARKET_750" }) => (
  request(`/api/swing/precheck/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}?index_name=${encodeURIComponent(indexName)}`)
);
export const getMomentumPrecheck = ({ exchange = "NSE", symbol, indexName = "BROAD_MARKET_750" }) => (
  request(`/api/momentum/precheck/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}?index_name=${encodeURIComponent(indexName)}`)
);
export const getMomentumTvConfirmed = ({ indexName = "BROAD_MARKET_750", limit } = {}) => (
  request(`/api/momentum/tv-confirmed?index_name=${encodeURIComponent(indexName)}${limit ? `&limit=${encodeURIComponent(limit)}` : ""}`)
);
export const testTvSymbol = (symbol, timeframe) => request("/api/tv/test-symbol", {
  method: "POST",
  body: JSON.stringify({ symbol, timeframe, fetch_candles: true }),
});
export const buildSwingSignals = (save = false) => request(`/api/signals/build-tv-confirmed?limit=5&timeframe=1D&save=${save}`, { method: "POST" });
export const buildMomentumSignals = (save = false) => request(`/api/signals/build-momentum-tv-confirmed?limit=5&timeframe=1D&save=${save}`, { method: "POST" });
export const buildPaperPlans = (signalType = "SWING_TV_CONFIRMED", save = false) => request(`/api/paper/build-plans?limit=5&timeframe=1D&save=${save}&signal_type=${encodeURIComponent(signalType)}`, { method: "POST" });
export const updatePaperPlans = (maxTrades) => request(`/api/paper/update-trades${maxTrades ? `?max_trades=${encodeURIComponent(maxTrades)}` : ""}`, { method: "POST" });
export const runPaperUpdateDryRun = ({ maxTrades = 6, maxWrites = 1 } = {}) => request(`/api/paper/update-trades?dry_run=true&max_trades=${encodeURIComponent(maxTrades)}&max_writes=${encodeURIComponent(maxWrites)}`, { method: "POST" });
export const approvePaperUpdateFromDryRun = ({
  approvedDryRunId,
  confirmationText,
  maxTrades = 6,
  maxWrites = 1,
}) => request("/api/paper/update-trades/approve", {
  method: "POST",
  body: JSON.stringify({
    approved_dry_run_id: approvedDryRunId,
    confirmation_text: confirmationText,
    max_trades: maxTrades,
    max_writes: maxWrites,
  }),
});
export const getPaperUpdateProgress = () => request("/api/paper/update-progress");
export const getPaperUpdateRuns = (limit = 10) => request(`/api/paper/update-runs?limit=${encodeURIComponent(limit)}`);
export const getPaperUpdateLock = () => request("/api/paper/update-lock");
export const getPaperUpdateSchedulerStatus = () => request("/api/paper/update-scheduler/status");
export const getPaperSummary = () => request("/api/paper/summary");
export const getPaperEquity = () => request("/api/dashboard/paper-equity");
export const getPaperSignals = () => request("/api/signals/paper");
export const getPaperPlans = () => request("/api/paper/plans?source_signal_type=ALL");
export const getActiveTrades = () => request("/api/paper/active");
export const getAllTrades = () => request("/api/paper/trades");
export const resetBuildPaperFromTradeReady = (dryRun = true) => request(`/api/paper/reset-build-trade-ready?index_name=BROAD_MARKET_750&dry_run=${dryRun}`, { method: "POST" });
export const loadAllMarketData = (dryRun = false) => request(`/api/market/load-all?index_name=BROAD_MARKET_750&dry_run=${dryRun}`, { method: "POST" });
export const getMarketLoadProgress = () => request("/api/market/load-progress?index_name=BROAD_MARKET_750");
export const runPaperPipeline = ({ limit = 1, timeframe = "1D", dryRun = true, strategy = "swing" }) => (
  request(`/api/paper/run-pipeline?limit=${limit}&timeframe=${encodeURIComponent(timeframe)}&dry_run=${dryRun}&strategy=${strategy}`, { method: "POST" })
);
export const momentumTvConfirm = ({
  indexName = "BROAD_MARKET_750",
  limit = 1,
  offset = 0,
  batchNumber,
  batchSize,
  timeframes = "1D,4H,1H",
  save = true,
  forceUseStaleScores = false,
  singleSymbol = false,
  symbol,
  tradingviewSymbol,
  exchange = "NSE",
} = {}) => (
  request(`/api/momentum/tv-confirm?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}${batchNumber ? `&batch_number=${encodeURIComponent(batchNumber)}` : ""}${batchSize ? `&batch_size=${encodeURIComponent(batchSize)}` : ""}&timeframes=${encodeURIComponent(timeframes)}&save=${encodeURIComponent(save)}&force_use_stale_scores=${encodeURIComponent(forceUseStaleScores)}&single_symbol=${encodeURIComponent(singleSymbol)}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}${tradingviewSymbol ? `&tradingview_symbol=${encodeURIComponent(tradingviewSymbol)}` : ""}&exchange=${encodeURIComponent(exchange)}`, { method: "POST" })
);
export const swingTvConfirm = ({
  indexName = "BROAD_MARKET_750",
  limit = 1,
  offset = 0,
  batchNumber,
  batchSize,
  timeframes = "1W,1D,4H,1H",
  save = false,
  singleSymbol = false,
  symbol,
  tradingviewSymbol,
  exchange = "NSE",
} = {}) => (
  request(`/api/swing/tv-confirm?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}${batchNumber ? `&batch_number=${encodeURIComponent(batchNumber)}` : ""}${batchSize ? `&batch_size=${encodeURIComponent(batchSize)}` : ""}&timeframes=${encodeURIComponent(timeframes)}&save=${encodeURIComponent(save)}&single_symbol=${encodeURIComponent(singleSymbol)}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}${tradingviewSymbol ? `&tradingview_symbol=${encodeURIComponent(tradingviewSymbol)}` : ""}&exchange=${encodeURIComponent(exchange)}`, { method: "POST" })
);
