import inspect
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from services.historical_market_repository import (
    HistoricalMarketRepository,
    COLLECTION_NAME,
    TIMEFRAME_SECONDS,
    utc_now,
)

logger = logging.getLogger(__name__)


class HistoricalSyncEngine:
    """
    Historical Synchronization & Self-Healing Recovery Engine (HSE) for:
    - Incremental candle updates
    - Staleness checking & health scanning
    - Automatic gap recovery
    - Data integrity verification & auto-repair
    - Recovery telemetry tracking
    """

    def __init__(self, db: Any):
        self.db = db
        self.telemetry = {
            "symbols_processed": 0,
            "new_candles_appended": 0,
            "duplicate_candles_skipped": 0,
            "failed_updates": 0,
            "gap_detections": 0,
            "db_writes": 0,
            "total_sync_duration_ms": 0.0,
            "append_latencies_ms": [],
            "buckets_scanned": 0,
            "healthy_buckets": 0,
            "stale_buckets": 0,
            "gap_buckets": 0,
            "gap_recoveries_attempted": 0,
            "successful_recoveries": 0,
            "failed_recoveries": 0,
            "duplicate_removals": 0,
            "metadata_repairs": 0,
            "scan_duration_ms": 0.0,
        }

    def reset_telemetry(self) -> None:
        self.telemetry = {
            "symbols_processed": 0,
            "new_candles_appended": 0,
            "duplicate_candles_skipped": 0,
            "failed_updates": 0,
            "gap_detections": 0,
            "db_writes": 0,
            "total_sync_duration_ms": 0.0,
            "append_latencies_ms": [],
            "buckets_scanned": 0,
            "healthy_buckets": 0,
            "stale_buckets": 0,
            "gap_buckets": 0,
            "gap_recoveries_attempted": 0,
            "successful_recoveries": 0,
            "failed_recoveries": 0,
            "duplicate_removals": 0,
            "metadata_repairs": 0,
            "scan_duration_ms": 0.0,
        }

    @staticmethod
    def _get_collection(db: Any):
        if hasattr(db, "get_collection"):
            return db.get_collection(COLLECTION_NAME)
        return db[COLLECTION_NAME]

    async def sync_stock(
        self,
        symbol: str,
        timeframe: str,
        incoming_candles: List[Dict[str, Any]],
        source: str = "TradingView",
    ) -> Dict[str, Any]:
        """
        Executes single-stock incremental synchronization.
        """
        start_t = time.perf_counter()

        res = await HistoricalMarketRepository.sync_candles_incremental(
            self.db, symbol, timeframe, incoming_candles, source=source
        )

        elapsed_ms = (time.perf_counter() - start_t) * 1000.0
        res["duration_ms"] = elapsed_ms
        return res

    async def run_batch_sync(
        self,
        symbol_payloads: Dict[tuple[str, str], List[Dict[str, Any]]],
        source: str = "TradingView",
    ) -> Dict[str, Any]:
        """
        Runs incremental sync for a dictionary mapping (symbol, timeframe) -> incoming_candles.
        Updates internal telemetry.
        """
        start_batch = time.perf_counter()

        for (sym, tf), candles in symbol_payloads.items():
            self.telemetry["symbols_processed"] += 1
            t_start = time.perf_counter()

            res = await self.sync_stock(sym, tf, candles, source=source)

            t_elapsed = (time.perf_counter() - t_start) * 1000.0
            self.telemetry["append_latencies_ms"].append(t_elapsed)

            status = res.get("status")
            if status == "SUCCESS":
                self.telemetry["new_candles_appended"] += res.get("appended_count", 0)
                self.telemetry["duplicate_candles_skipped"] += res.get("duplicates_skipped", 0)
                if res.get("db_writes", 0) > 0:
                    self.telemetry["db_writes"] += 1
                if res.get("sync_status") == "GAP_DETECTED":
                    self.telemetry["gap_detections"] += 1
            elif status == "UNCHANGED":
                self.telemetry["duplicate_candles_skipped"] += res.get("duplicates_skipped", 0)
            else:
                self.telemetry["failed_updates"] += 1

        batch_elapsed_ms = (time.perf_counter() - start_batch) * 1000.0
        self.telemetry["total_sync_duration_ms"] = batch_elapsed_ms

        latencies = self.telemetry["append_latencies_ms"]
        avg_latency = (sum(latencies) / len(latencies)) if latencies else 0.0

        return {
            "symbols_processed": self.telemetry["symbols_processed"],
            "new_candles_appended": self.telemetry["new_candles_appended"],
            "duplicate_candles_skipped": self.telemetry["duplicate_candles_skipped"],
            "failed_updates": self.telemetry["failed_updates"],
            "gap_detections": self.telemetry["gap_detections"],
            "db_writes": self.telemetry["db_writes"],
            "avg_append_latency_ms": round(avg_latency, 3),
            "total_sync_duration_ms": round(batch_elapsed_ms, 3),
        }

    async def run_health_scan(self, current_timestamp: Optional[int] = None) -> Dict[str, Any]:
        """
        Metadata-first health scan across all historical_market_data documents.
        Checks last_timestamp freshness, gap days/bars, and updates sync_status.
        """
        start_t = time.perf_counter()

        now_ts = current_timestamp if current_timestamp is not None else int(utc_now().timestamp())
        coll = self._get_collection(self.db)

        cursor = coll.find(
            {},
            {
                "symbol": 1,
                "timeframe": 1,
                "last_timestamp": 1,
                "candle_count": 1,
                "sync_status": 1,
                "_id": 0,
            }
        )

        docs = []
        if hasattr(cursor, "to_list"):
            docs = await cursor.to_list(length=10000)
        else:
            docs = list(cursor)

        stale_list = []
        gap_list = []

        for d in docs:
            self.telemetry["buckets_scanned"] += 1
            sym = d.get("symbol")
            tf = d.get("timeframe")
            last_ts = d.get("last_timestamp")

            if not last_ts:
                self.telemetry["gap_buckets"] += 1
                gap_list.append({"symbol": sym, "timeframe": tf, "reason": "NO_TIMESTAMP"})
                continue

            gap_seconds = max(0, now_ts - last_ts)
            tf_secs = TIMEFRAME_SECONDS.get(tf, 86400)

            missing_days = gap_seconds // 86400
            missing_bars = gap_seconds // tf_secs

            if missing_bars > 1:
                status = "GAP_DETECTED" if missing_days >= 3 else "STALE"
                if status == "GAP_DETECTED":
                    self.telemetry["gap_buckets"] += 1
                    gap_list.append({"symbol": sym, "timeframe": tf, "missing_days": missing_days, "missing_bars": missing_bars})
                else:
                    self.telemetry["stale_buckets"] += 1
                    stale_list.append({"symbol": sym, "timeframe": tf, "missing_days": missing_days, "missing_bars": missing_bars})

                await HistoricalMarketRepository.update_metadata(
                    self.db,
                    sym,
                    tf,
                    sync_status=status,
                    missing_gap_days=missing_days,
                    missing_gap_bars=missing_bars,
                )
            else:
                self.telemetry["healthy_buckets"] += 1

        scan_duration_ms = (time.perf_counter() - start_t) * 1000.0
        self.telemetry["scan_duration_ms"] = round(scan_duration_ms, 3)

        return {
            "buckets_scanned": self.telemetry["buckets_scanned"],
            "healthy_buckets": self.telemetry["healthy_buckets"],
            "stale_buckets": self.telemetry["stale_buckets"],
            "gap_buckets": self.telemetry["gap_buckets"],
            "scan_duration_ms": self.telemetry["scan_duration_ms"],
            "stale_list": stale_list,
            "gap_list": gap_list,
        }

    async def recover_gap(
        self,
        symbol: str,
        timeframe: str,
        fetch_candles_fn: Optional[Callable[[str, str], List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        """
        Self-healing gap recovery: fetches missing candles and appends incrementally.
        Resets sync_status = 'HEALTHY' if recovery succeeds.
        """
        self.telemetry["gap_recoveries_attempted"] += 1
        clean_sym = symbol.strip().upper()
        clean_tf = timeframe.strip().upper()

        if fetch_candles_fn is None:
            self.telemetry["failed_recoveries"] += 1
            await HistoricalMarketRepository.update_metadata(
                self.db,
                clean_sym,
                clean_tf,
                last_failed_sync=utc_now(),
                error_reason="NO_FETCH_PROVIDER",
            )
            return {
                "symbol": clean_sym,
                "timeframe": clean_tf,
                "status": "RECOVERY_FAILED",
                "error": "NO_FETCH_PROVIDER",
            }

        try:
            missing_candles = fetch_candles_fn(clean_sym, clean_tf)
            if inspect.iscoroutine(missing_candles):
                missing_candles = await missing_candles

            res = await HistoricalMarketRepository.sync_candles_incremental(
                self.db, clean_sym, clean_tf, missing_candles
            )

            if res.get("status") in {"SUCCESS", "UNCHANGED"}:
                self.telemetry["successful_recoveries"] += 1
                await HistoricalMarketRepository.update_metadata(
                    self.db,
                    clean_sym,
                    clean_tf,
                    sync_status="HEALTHY",
                    missing_gap_days=0,
                    missing_gap_bars=0,
                    last_successful_sync=utc_now(),
                    error_reason=None,
                )
                return {
                    "symbol": clean_sym,
                    "timeframe": clean_tf,
                    "status": "RECOVERY_SUCCESS",
                    "appended_count": res.get("appended_count", 0),
                }
            else:
                self.telemetry["failed_recoveries"] += 1
                return {
                    "symbol": clean_sym,
                    "timeframe": clean_tf,
                    "status": "RECOVERY_FAILED",
                    "reason": res.get("status"),
                }

        except Exception as exc:
            self.telemetry["failed_recoveries"] += 1
            logger.error(f"Gap recovery exception for {clean_sym} {clean_tf}: {exc}")
            await HistoricalMarketRepository.update_metadata(
                self.db,
                clean_sym,
                clean_tf,
                last_failed_sync=utc_now(),
                error_reason=str(exc),
            )
            return {
                "symbol": clean_sym,
                "timeframe": clean_tf,
                "status": "RECOVERY_EXCEPTION",
                "error": str(exc),
            }

    async def recover_manual(
        self,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        fetch_candles_fn: Optional[Callable[[str, str], List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        """
        Manual recovery interface for single-symbol, single-timeframe, or full-market recovery.
        """
        coll = self._get_collection(self.db)
        query = {}
        if symbol:
            query["symbol"] = symbol.strip().upper()
        if timeframe:
            query["timeframe"] = timeframe.strip().upper()

        cursor = coll.find(query, {"symbol": 1, "timeframe": 1, "_id": 0})
        docs = await cursor.to_list(length=10000) if hasattr(cursor, "to_list") else list(cursor)

        results = []
        for d in docs:
            r = await self.recover_gap(d["symbol"], d["timeframe"], fetch_candles_fn=fetch_candles_fn)
            results.append(r)

        return {
            "total_attempted": len(results),
            "successful": sum(1 for r in results if r.get("status") == "RECOVERY_SUCCESS"),
            "failed": sum(1 for r in results if r.get("status") != "RECOVERY_SUCCESS"),
            "details": results,
        }
