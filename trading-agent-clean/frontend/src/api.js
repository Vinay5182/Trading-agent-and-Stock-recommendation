export const validateApiBase = (url) => {
  if (!url) return "http://127.0.0.1:8011";
  const trimmed = url.trim();
  if (trimmed.startsWith("/")) {
    return trimmed;
  }
  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch (err) {
    throw new Error("Invalid API base configuration: VITE_API_BASE must be a valid URL or relative path.");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("Only http: and https: protocols are supported.");
  }
  return trimmed;
};

const getApiBase = () => {
  let envValue;
  try {
    envValue = import.meta.env?.VITE_API_BASE;
  } catch (e) {
    envValue = typeof process !== "undefined" ? process.env?.VITE_API_BASE : undefined;
  }
  if (envValue !== undefined && envValue !== "") {
    return validateApiBase(envValue);
  }
  return "http://127.0.0.1:8011";
};

export const API_BASE = getApiBase();
export const OPERATOR_INTENT_HEADER = "X-Trading-Agent-Intent";
export const OPERATOR_INTENT_VALUE = "operator-write-v1";

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

const withOperatorIntent = (options = {}, enabled = true) => (
  enabled ? { ...options, operatorIntent: true } : options
);

async function request(path, options = {}) {
  const { timeoutMs = 120000, signal, operatorIntent = false, ...fetchOptions } = options;
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeoutId = globalThis.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = {
      "Content-Type": "application/json",
      ...(operatorIntent ? { [OPERATOR_INTENT_HEADER]: OPERATOR_INTENT_VALUE } : {}),
      ...(fetchOptions.headers || {}),
    };
    const response = await fetch(`${API_BASE}${path}`, {
      cache: "no-store",
      ...fetchOptions,
      headers,
      signal: controller.signal,
    });

    const maxLimit = 2 * 1024 * 1024; // 2MB limit
    const contentLength = response.headers && typeof response.headers.get === "function"
      ? response.headers.get("Content-Length")
      : null;
    if (contentLength) {
      const parsedLength = parseInt(contentLength, 10);
      if (!isNaN(parsedLength) && parsedLength > maxLimit) {
        throw new Error(`Response body size limit exceeded: ${parsedLength} bytes exceeds the maximum allowed limit.`);
      }
    }

    let text = "";
    if (response.body && typeof response.body.getReader === "function") {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let bytesRead = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        bytesRead += value.length;
        if (bytesRead > maxLimit) {
          reader.cancel();
          throw new Error("Response body size limit exceeded.");
        }
        text += decoder.decode(value, { stream: true });
      }
      text += decoder.decode();
    } else {
      text = await response.text();
      if (text && text.length > maxLimit) {
        throw new Error("Response body size limit exceeded.");
      }
    }

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
      throw new Error(`Backend request failed. Check that the API on ${API_BASE} is running.`);
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
export const getCandidateOutcomesCalendar = ({ year } = {}, options = {}) => {
  const query = year ? `?year=${encodeURIComponent(year)}` : "";
  return request(`/api/ai/candidate-outcomes/calendar${query}`, options);
};
export const getDailyDatasetRows = ({ tradeDate, limit = 500 } = {}, options = {}) => {
  const params = new URLSearchParams();
  if (tradeDate) {
    params.set("trade_date_from", tradeDate);
    params.set("trade_date_to", tradeDate);
  }
  if (limit) params.set("limit", String(limit));
  const query = params.toString();
  return request(`/api/ai/daily-dataset/rows${query ? `?${query}` : ""}`, options);
};
export const getScanRows = (scanRunId, options = {}) => request(`/api/scan/rows${scanRunId ? `?scan_run_id=${encodeURIComponent(scanRunId)}` : ""}`, options);
export const runScoring = (indexName = "BROAD_MARKET_750", dryRun = false) => (
  request(
    `/api/score/run?index_name=${encodeURIComponent(indexName)}&dry_run=${encodeURIComponent(dryRun)}`,
    withOperatorIntent({ method: "POST" }, dryRun === false)
  )
);
export const getScoreSummary = (indexName = "BROAD_MARKET_750", options = {}) => request(`/api/score/summary?index_name=${encodeURIComponent(indexName)}`, options);
export const getSwingSummary = (indexName = "BROAD_MARKET_750", options = {}) => request(`/api/swing/summary?index_name=${encodeURIComponent(indexName)}`, options);
export const getSwingCandidates = (indexName = "BROAD_MARKET_750", limit = 100, options = {}) => request(`/api/swing/candidates?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}`, options);
export const getSwingTvConfirmed = ({ indexName = "BROAD_MARKET_750", limit } = {}, options = {}) => (
  request(`/api/swing/tv-confirmed?index_name=${encodeURIComponent(indexName)}${limit ? `&limit=${encodeURIComponent(limit)}` : ""}`, options)
);
export const getMomentumSummary = (indexName = "BROAD_MARKET_750", options = {}) => request(`/api/momentum/summary?index_name=${encodeURIComponent(indexName)}`, options);
export const getMomentumCandidates = (indexName = "BROAD_MARKET_750", limit = 100, options = {}) => request(`/api/momentum/candidates?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}`, options);
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
  operatorIntent: true,
});
export const getTradingViewRuntimeStatus = (options = {}) => request("/api/tv/runtime-status", options);
export function deriveTradingViewBusy(status) {
  if (!status) return false;
  return Boolean(
    status.recovering_from_timeout === true ||
    status.manager_available === false ||
    status.worker_running === true ||
    Number(status.queue_length || 0) > 0 ||
    status.active_operation != null
  );
}
export function tradingViewBadge(status) {
  if (!status) return { tone: "gray", label: "TradingView Unknown" };
  if (status.recovering_from_timeout === true) return { tone: "yellow", label: "TradingView Recovering" };
  if (status.last_error) return { tone: "red", label: "TradingView Error" };
  if (deriveTradingViewBusy(status)) {
    return { tone: "yellow", label: "TradingView Busy" };
  }
  if (status.connected) return { tone: "green", label: "TradingView Connected" };
  return { tone: "gray", label: "TradingView Idle" };
}
export function canStartTradingViewOperation(status) {
  if (!status) return false;
  if (deriveTradingViewBusy(status)) return false;
  if (status.preflight_ready === true) return true;
  return Boolean(
    status.cdp_reachable === true &&
    status.valid_chart_target_count === 1 &&
    status.manual_attachment_required === false &&
    status.preflight_code === "TV_TAB_NOT_ATTACHED"
  );
}
export function isBatchReady(status, lastUpdatedTime) {
  if (!canStartTradingViewOperation(status)) return false;
  if (!lastUpdatedTime) return false;
  const elapsed = Date.now() - lastUpdatedTime;
  return elapsed <= 10000;
}
export const getTradingViewAttachableTabs = (options = {}) => request("/api/tv/attachable-tabs", options);
export const attachTradingViewTab = (targetId, options = {}) => request(`/api/tv/attach-tab?target_id=${encodeURIComponent(targetId)}`, { method: "POST", ...withOperatorIntent(options) });
export const detachTradingViewTab = (options = {}) => request("/api/tv/detach-tab", { method: "POST", ...withOperatorIntent(options) });
export const getPaperUpdateProgress = (options = {}) => request("/api/paper/update-progress", options);
export const getPaperUpdateRuns = (limit = 10, options = {}) => request(`/api/paper/update-runs?limit=${encodeURIComponent(limit)}`, options);
export const getPaperUpdateLock = (options = {}) => request("/api/paper/update-lock", options);
export const getPaperUpdateSchedulerStatus = (options = {}) => request("/api/paper/update-scheduler/status", options);
export const getPaperSummary = (options = {}) => request("/api/paper/summary", options);
export const getPaperOpenTrades = (options = {}) => request("/api/paper/open", options);
export const getPaperHistory = (options = {}) => request("/api/paper/history", options);

export const loadAllMarketData = (dryRun = true) => request(`/api/market/load-all?index_name=BROAD_MARKET_750&dry_run=${dryRun}`, withOperatorIntent({ method: "POST" }, dryRun === false));
export const getMarketLoadProgress = () => request("/api/market/load-progress?index_name=BROAD_MARKET_750");
export const getMarketPipelineStatus = (options = {}) => request("/api/market/pipeline-status", options);
export const runPaperPipeline = ({ limit = 1, timeframe = "1D", dryRun = true, strategy = "swing" }) => (
  request(`/api/paper/run-pipeline?limit=${limit}&timeframe=${encodeURIComponent(timeframe)}&dry_run=${dryRun}&strategy=${strategy}`, withOperatorIntent({ method: "POST" }, dryRun === false))
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
  request(`/api/momentum/tv-confirm?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}${batchNumber ? `&batch_number=${encodeURIComponent(batchNumber)}` : ""}${batchSize ? `&batch_size=${encodeURIComponent(batchSize)}` : ""}&timeframes=${encodeURIComponent(timeframes)}&save=${encodeURIComponent(save)}&force_use_stale_scores=${encodeURIComponent(forceUseStaleScores)}&single_symbol=${encodeURIComponent(singleSymbol)}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}${tradingviewSymbol ? `&tradingview_symbol=${encodeURIComponent(tradingviewSymbol)}` : ""}&exchange=${encodeURIComponent(exchange)}`, withOperatorIntent({ method: "POST" }, save === true))
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
  request(`/api/swing/tv-confirm?index_name=${encodeURIComponent(indexName)}&limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}${batchNumber ? `&batch_number=${encodeURIComponent(batchNumber)}` : ""}${batchSize ? `&batch_size=${encodeURIComponent(batchSize)}` : ""}&timeframes=${encodeURIComponent(timeframes)}&save=${encodeURIComponent(save)}&single_symbol=${encodeURIComponent(singleSymbol)}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}${tradingviewSymbol ? `&tradingview_symbol=${encodeURIComponent(tradingviewSymbol)}` : ""}&exchange=${encodeURIComponent(exchange)}`, withOperatorIntent({ method: "POST" }, save === true))
);

export const getTradeOutcome = (tradeId, options = {}) => request(`/api/outcomes/trade/${encodeURIComponent(tradeId)}`, options);
export const evaluateAllTradeOutcomes = (options = {}) => request("/api/outcomes/evaluate-all", { method: "POST", ...options });

export function mapTimeframeToInterval(timeframe) {
  if (!timeframe) return "D";
  const clean = String(timeframe).trim().toUpperCase();
  if (clean === "1W" || clean === "W" || clean === "1WEEK" || clean === "WEEK") return "W";
  if (clean === "1D" || clean === "D" || clean === "1DAY" || clean === "DAY") return "D";
  if (clean === "4H" || clean === "240" || clean === "4HOUR") return "240";
  if (clean === "1H" || clean === "60" || clean === "1HOUR") return "60";
  if (clean === "1M" || clean === "M" || clean === "1MONTH" || clean === "MONTH") return "M";
  return clean || "D";
}

export function buildTradingViewSymbol(row) {
  if (!row) return "";
  let exchange = String(row.exchange || "").trim().toUpperCase();
  let rawSymbol = String(row.tradingview_symbol || row.symbol || row.canonical_symbol || "").trim();
  if (!rawSymbol || rawSymbol === "-" || rawSymbol === "null" || rawSymbol === "undefined") {
    return "";
  }
  if (rawSymbol.includes(":")) {
    const parts = rawSymbol.split(":");
    if (parts[0].trim()) {
      exchange = parts[0].trim().toUpperCase();
    }
    rawSymbol = parts.slice(1).join(":").trim();
  }
  if (!exchange) {
    exchange = "NSE";
  }
  let cleanSymbol = rawSymbol.toUpperCase();
  for (const suffix of [".NS", ".BO", "-EQ"]) {
    if (cleanSymbol.endsWith(suffix)) {
      cleanSymbol = cleanSymbol.slice(0, -suffix.length);
    }
  }
  cleanSymbol = cleanSymbol.replace(/-/g, "_").replace(/\s+/g, "");
  return `${exchange}:${cleanSymbol}`;
}

export function buildTradingViewUrl(row) {
  const tradingviewSymbol = buildTradingViewSymbol(row);
  if (!tradingviewSymbol) return "";
  const timeframe = row?.timeframe || row?.interval || row?.setup_timeframe || row?.timeframe_entry || "1D";
  const interval = mapTimeframeToInterval(timeframe);
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tradingviewSymbol)}&interval=${encodeURIComponent(interval)}`;
}

