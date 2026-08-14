# Historical Market Data Storage Design Specification

**Document Version:** 1.0.0  
**Status:** Approved Phase A Implementation  
**Collection Name:** `historical_market_data`  
**Repository Module:** `backend/services/historical_market_repository.py`

---

## 1. Executive Summary

This specification defines the foundation layer for the next-generation historical market data storage architecture (`historical_market_data`). 

Instead of storing individual market candles as separate MongoDB documents (the legacy `market_candles` model), `historical_market_data` uses a **Bucket Document-per-Stock-per-Timeframe** architecture (`1 Document = 1 Symbol + 1 Timeframe`).

---

## 2. Document Schema & Data Model

```json
{
  "_id": ObjectId("6a5e0edd18c97b754c27a66e"),
  "symbol": "NSE:ABB",
  "canonical_symbol": "ABB",
  "exchange": "NSE",
  "timeframe": "1D",
  "source": "TradingView",
  "first_timestamp": 1603683900,
  "last_timestamp": 1784886300,
  "candle_count": 1410,
  "created_at": ISODate("2026-07-20T12:04:45.054Z"),
  "last_updated": ISODate("2026-07-25T16:00:00.000Z"),
  "sync_status": "HEALTHY",
  "sync_attempts": 0,
  "missing_gap_days": 0,
  "missing_gap_bars": 0,
  "last_successful_sync": ISODate("2026-07-25T16:00:00.000Z"),
  "last_failed_sync": null,
  "error_reason": null,
  "candles": [
    {
      "timestamp": 1603683900,
      "open": 100.0,
      "high": 120.0,
      "low": 100.0,
      "close": 118.84,
      "volume": 185333.0
    }
  ]
}
```

### Schema Field Dictionary

| Field | Type | Description |
| :--- | :---: | :--- |
| `symbol` | `str` | Primary symbol identifier with exchange prefix (e.g. `"NSE:ABB"`). |
| `canonical_symbol` | `str` | Ticker symbol without exchange prefix (e.g. `"ABB"`). |
| `exchange` | `str` | Market exchange (`"NSE"`). |
| `timeframe` | `str` | Bar interval (`"1D"`, `"1H"`, `"4H"`, `"1W"`). |
| `source` | `str` | Data provider name (`"TradingView"`). |
| `first_timestamp` | `int` | Unix epoch seconds of earliest candle in array. |
| `last_timestamp` | `int` | Unix epoch seconds of newest candle in array. |
| `candle_count` | `int` | Pre-calculated length of `candles` array. |
| `created_at` | `datetime` | UTC document creation timestamp. |
| `last_updated` | `datetime` | UTC timestamp of last array modification. |
| `sync_status` | `str` | Operational sync state: `["HEALTHY", "STALE", "GAP_DETECTED", "RECOVERING", "SYNC_FAILED", "UNRECOVERABLE_GAP"]`. |
| `sync_attempts` | `int` | Consecutive retry failure counter. |
| `missing_gap_days` | `int` | Calendar days gap between `last_timestamp` and current date. |
| `missing_gap_bars` | `int` | Estimated missing bar count for timeframe. |
| `last_successful_sync` | `datetime` | UTC timestamp of last successful bar append. |
| `last_failed_sync` | `datetime` | UTC timestamp of last network/db failure. |
| `error_reason` | `str` | Diagnostic error message string. |
| `candles` | `list[dict]` | Embedded list of OHLCV bars sorted by `timestamp` ascending. |

---

## 3. Database Indexes

Registered centrally in [mongo_indexes.py](file:///c:/Users/Asus/OneDrive/Documents/Trading_Strategy/trading-agent-clean/backend/services/mongo_indexes.py):

1. **`historical_market_data_unique_identity`**:  
   Keys: `{ "symbol": 1, "timeframe": 1 }` (**UNIQUE**, Critical).  
   *Ensures exactly 1 document per stock per timeframe.*
2. **`historical_market_data_canonical`**:  
   Keys: `{ "canonical_symbol": 1, "timeframe": 1 }`.  
   *Enables cross-collection queries using raw symbols.*
3. **`historical_market_data_last_updated`**:  
   Keys: `{ "last_updated": 1 }`.  
   *Supports freshness audits and staleness sorting.*

---

## 4. Repository Layer Interface (`HistoricalMarketRepository`)

Module: [historical_market_repository.py](file:///c:/Users/Asus/OneDrive/Documents/Trading_Strategy/trading-agent-clean/backend/services/historical_market_repository.py)

Key Methods:
- `create_document(db, symbol, timeframe, ...)`: Initializes an empty bucket document.
- `append_candles(db, symbol, timeframe, candles, ...)`: Merges new bars into the array with in-memory timestamp deduplication, chronological sorting, and atomic Mongo `$set` / `$setOnInsert` updates.
- `get_document(db, symbol, timeframe)`: Retrieves document for a symbol/timeframe.
- `get_last_timestamp(db, symbol, timeframe)`: Reads integer `last_timestamp` from header.
- `update_metadata(db, symbol, timeframe, ...)`: Updates operational sync telemetry.
- `detect_gap(db, symbol, timeframe, current_timestamp)`: Computes staleness metrics and gap severity.

---

## 5. Zero-Downtime Migration Strategy

```
Phase A: Foundation Layer (COMPLETED)
Phase B: Service Abstraction Layer (Wrap readers into get_candles())
Phase C: ETL Data Migration Script (Group 180k legacy docs -> 187 bucket docs)
Phase D: Verification & Parity Audit (Automated equality testing)
Phase E: Cutover & Legacy Cleanup (Switch alias, archive legacy market_candles)
```

- **Phase A Guarantee:** Zero production code changes. Active ingestion and reading paths continue to operate against legacy `market_candles` with 100% backward compatibility.
