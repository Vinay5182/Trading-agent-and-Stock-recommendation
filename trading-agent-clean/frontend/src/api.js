export const API_BASE = "http://127.0.0.1:8011";

export class RequestCancelledError extends Error {
  constructor(message = "Request cancelled.") {
    super(message);
    this.name = "AbortError";
    this.code = "REQUEST_CANCELLED";
    this.cancelled = true;
  }
}

export const isRequestCancellation = (error) => {
  const message = String(error?.message || error || "");
  return Boolean(
    error?.cancelled === true
    || error?.code === "REQUEST_CANCELLED"
    || error?.name === "AbortError"
    || /^Request cancelled\.?$/i.test(message)
  );
};

async function request(path, options = {}) {
  const { timeoutMs = 120000, signal, ...fetchOptions } = options;
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeoutId = globalThis.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: { "Content-Type": "application/json", ...(fetchOptions.headers || {}) },
      cache: "no-store",
      ...fetchOptions,
      signal: controller.signal,
    });
    const text = await response.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = text ? { raw: text, parse_error: true } : {};
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
  } catch (error) {
    if (controller.signal.aborted) {
      if (signal?.aborted) throw new RequestCancelledError();
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)} seconds.`);
    }
    if (isRequestCancellation(error)) throw new RequestCancelledError();
    if (error instanceof TypeError) {
      throw new Error("Backend request failed. Check that the API on 127.0.0.1:8011 is running.");
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeoutId);
    signal?.removeEventListener("abort", abortFromCaller);
  }
}

export const getHealth = (options = {}) => request("/health", options);
export const getSettings = (options = {}) => request("/api/settings", options);
export const getSystemRuntimeInfo = (options = {}) => request("/api/system/runtime-info", options);
export const getAiFeatureDatasetSummary = ({ strategyType, timeframe } = {}, options = {}) => {
  const params = new URLSearchParams();
  if (strategyType) params.set("strategy_type", strategyType);
  if (timeframe) params.set("timeframe", timeframe);
  const query = params.toString();
  return request(`/api/ai/features/summary${query ? `?${query}` : ""}`, options);
};
export const getAiDataCollectionStatus = (options = {}) => request("/api/ai/features/collection-status", options);
export const getAiFeatureSnapshots = (limit = 50, options = {}) => request(`/api/ai/features/snapshots?limit=${encodeURIComponent(limit)}`, options);
export const getAiOutcomePreview = (limit = 50, options = {}) => request(`/api/ai/features/outcome-preview?limit=${encodeURIComponent(limit)}`, options);
export const runScan = (options = {}) => request("/api/scan", {
  method: "POST",
  body: JSON.stringify({ selected_index: "DEFAULT_UNIVERSE", limit: 50, force_refresh: false }),
  ...options,
});
export const getScanRows = (scanRunId, options = {}) => request(`/api/scan/rows${scanRunId ? `?scan_run_id=${encodeURIComponent(scanRunId)}` : ""}`, options);
export const runScoring = (indexName = "BROAD_MARKET_750") => request(`/api/score/run?index_name=${encodeURIComponent(indexName)}`, { method: "POST" });
export const getScoreSummary = (indexName = "BROAD_MARKET_750", options = {}) => request(`/api/score/summary?index_name=${encodeURIComponent(indexName)}`, options);
export const getSwingSummary = (indexName = "BROAD_MARKET_750", options = {}) => request(`/api/swing/summary?index_name=${encodeURIComponent(indexName)}`, options);
export const getSwingCandidates = (indexName = "BROAD_MARKET_750", limit = 100) => request(`/api/swing/candidates?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}`);
export const getSwingTvConfirmed = ({ indexName = "BROAD_MARKET_750", limit } = {}, options = {}) => (
  request(`/api/swing/tv-confirmed?index_name=${encodeURIComponent(indexName)}${limit ? `&limit=${encodeURIComponent(limit)}` : ""}`, options)
);
export const getMomentumSummary = (indexName = "BROAD_MARKET_750", options = {}) => request(`/api/momentum/summary?index_name=${encodeURIComponent(indexName)}`, options);
export const getMomentumCandidates = (indexName = "BROAD_MARKET_750", limit = 100) => request(`/api/momentum/candidates?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}`);
export const getMarketDataSymbol = ({ exchange = "NSE", symbol }, options = {}) => request(`/api/market/data/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}`, options);
export const getSwingPrecheck = ({ exchange = "NSE", symbol, indexName = "BROAD_MARKET_750" }, options = {}) => (
  request(`/api/swing/precheck/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}?index_name=${encodeURIComponent(indexName)}`, options)
);
export const getMomentumPrecheck = ({ exchange = "NSE", symbol, indexName = "BROAD_MARKET_750" }, options = {}) => (
  request(`/api/momentum/precheck/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}?index_name=${encodeURIComponent(indexName)}`, options)
);
export const getMomentumTvConfirmed = ({ indexName = "BROAD_MARKET_750", limit } = {}, options = {}) => (
  request(`/api/momentum/tv-confirmed?index_name=${encodeURIComponent(indexName)}${limit ? `&limit=${encodeURIComponent(limit)}` : ""}`, options)
);
export const getDashboardPaperEquity = (options = {}) => request("/api/dashboard/paper-equity", options);
export const testTvSymbol = (symbol, timeframe) => request("/api/tv/test-symbol", {
  method: "POST",
  body: JSON.stringify({ symbol, timeframe, fetch_candles: true }),
});
export const buildSwingSignals = (save = false) => request(`/api/signals/build-tv-confirmed?limit=5&timeframe=1D&save=${save}`, { method: "POST" });
export const buildMomentumSignals = (save = false) => request(`/api/signals/build-momentum-tv-confirmed?limit=5&timeframe=1D&save=${save}`, { method: "POST" });
export const buildPaperPlans = (signalType = "SWING_TV_CONFIRMED", save = false) => request(`/api/paper/build-plans?limit=5&timeframe=1D&save=${save}&signal_type=${encodeURIComponent(signalType)}`, { method: "POST" });
export const getTradingViewRuntimeStatus = (options = {}) => request("/api/tv/runtime-status", options);
export const getTradingViewAttachableTabs = (options = {}) => request("/api/tv/attachable-tabs", options);
export const attachTradingViewTab = (targetId, options = {}) => request(`/api/tv/attach-tab?target_id=${encodeURIComponent(targetId)}`, { method: "POST", ...options });
export const detachTradingViewTab = (options = {}) => request("/api/tv/detach-tab", { method: "POST", ...options });
export const getPaperUpdateProgress = (options = {}) => request("/api/paper/update-progress", options);
export const getPaperUpdateRuns = (limit = 10, options = {}) => request(`/api/paper/update-runs?limit=${encodeURIComponent(limit)}`, options);
export const getPaperUpdateLock = (options = {}) => request("/api/paper/update-lock", options);
export const getPaperUpdateSchedulerStatus = (options = {}) => request("/api/paper/update-scheduler/status", options);
export const getPaperSummary = (options = {}) => request("/api/paper/summary", options);
export const getPaperOpenTrades = (options = {}) => request("/api/paper/open", options);
export const getPaperHistory = (options = {}) => request("/api/paper/history", options);
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
