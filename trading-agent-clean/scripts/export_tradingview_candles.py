"""Export raw TradingView OHLCV candles to CSV using the existing chart client."""

from __future__ import annotations

import argparse
import calendar
import csv
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "exports" / "tradingview_candles"
CSV_COLUMNS = [
    "symbol",
    "tradingview_symbol",
    "timeframe",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "exported_at",
]
INTERVALS = {"4H": "240", "1H": "60", "15M": "15", "5M": "5"}

sys.path.insert(0, str(BACKEND_DIR))

from tv_client import TradingViewClient, tradingview_url  # noqa: E402


@dataclass(frozen=True)
class ExportJob:
    symbol: str
    tradingview_symbol: str
    timeframe: str
    months: int = 0
    days: int = 0

    @property
    def key(self) -> str:
        return f"{self.symbol}_{self.timeframe}"

    @property
    def filename(self) -> str:
        return f"{self.key}.csv"

    def cutoff(self, now: datetime) -> datetime:
        if self.months:
            return subtract_months(now, self.months)
        return now - timedelta(days=self.days)


EXPORT_JOBS = [
    ExportJob("NIFTY", "NSE:NIFTY", "1H", months=6),
    ExportJob("NIFTY", "NSE:NIFTY", "15M", months=3),
    ExportJob("NIFTY", "NSE:NIFTY", "5M", months=1),
    ExportJob("GBPJPY", "OANDA:GBPJPY", "4H", months=6),
    ExportJob("GBPJPY", "OANDA:GBPJPY", "1H", months=3),
    ExportJob("GBPJPY", "OANDA:GBPJPY", "15M", months=1),
    ExportJob("GBPJPY", "OANDA:GBPJPY", "5M", days=14),
    ExportJob("EURUSD", "OANDA:EURUSD", "4H", months=6),
    ExportJob("EURUSD", "OANDA:EURUSD", "1H", months=3),
    ExportJob("EURUSD", "OANDA:EURUSD", "15M", months=1),
    ExportJob("EURUSD", "OANDA:EURUSD", "5M", days=14),
    ExportJob("GBPUSD", "OANDA:GBPUSD", "4H", months=6),
    ExportJob("GBPUSD", "OANDA:GBPUSD", "1H", months=3),
    ExportJob("GBPUSD", "OANDA:GBPUSD", "15M", months=1),
    ExportJob("GBPUSD", "OANDA:GBPUSD", "5M", days=14),
    ExportJob("XAUUSD", "OANDA:XAUUSD", "4H", months=6),
    ExportJob("XAUUSD", "OANDA:XAUUSD", "1H", months=3),
    ExportJob("XAUUSD", "OANDA:XAUUSD", "15M", months=1),
    ExportJob("XAUUSD", "OANDA:XAUUSD", "5M", days=14),
]


def subtract_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def candle_epoch_seconds(candle: dict[str, Any]) -> float | None:
    value = candle.get("time", candle.get("timestamp"))
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    while timestamp > 100_000_000_000:
        timestamp /= 1000
    return timestamp


def iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def wait_for_chart(
    client: TradingViewClient,
    tradingview_symbol: str,
    interval: str,
    timeout_seconds: int,
) -> None:
    deadline = time.time() + timeout_seconds
    last_symbol = None
    last_resolution = None
    while time.time() <= deadline:
        last_symbol = client.get_active_chart_symbol()
        last_resolution = client.get_active_chart_resolution()
        if client.symbol_matches(last_symbol, tradingview_symbol) and last_resolution == interval:
            return
        time.sleep(1)
    raise RuntimeError(
        f"Chart did not load requested symbol/resolution "
        f"(active_symbol={last_symbol}, active_resolution={last_resolution})"
    )


def request_visible_history(client: TradingViewClient, cutoff_epoch: int, end_epoch: int) -> Any:
    return client.evaluate_runtime(
        f"""
        (async () => {{
          const api = window.TradingViewApi;
          const chart = api && typeof api.activeChart === "function" ? api.activeChart() : null;
          if (!chart || typeof chart.setVisibleRange !== "function") {{
            throw new Error("TradingView active chart setVisibleRange is unavailable");
          }}
          const result = chart.setVisibleRange({{from: {cutoff_epoch}, to: {end_epoch}}});
          if (result && typeof result.then === "function") await result;
          return {{
            symbol: typeof chart.symbol === "function" ? chart.symbol() : null,
            resolution: typeof chart.resolution === "function" ? String(chart.resolution()) : null,
            visible_range: typeof chart.getVisibleRange === "function" ? chart.getVisibleRange() : null
          }};
        }})()
        """
    )


def request_more_bars(client: TradingViewClient, bar_count: int = 5000) -> Any:
    return client.evaluate_runtime(
        f"""
        (() => {{
          const api = window.TradingViewApi;
          const chart = api && typeof api.activeChart === "function" ? api.activeChart() : null;
          const model = chart && typeof chart.chartModel === "function" ? chart.chartModel() : null;
          const series = model && typeof model.mainSeries === "function" ? model.mainSeries() : null;
          if (!series || typeof series.requestMoreData !== "function") {{
            throw new Error("TradingView main series requestMoreData is unavailable");
          }}
          const before = typeof series.bars === "function" ? series.bars().size() : null;
          series.requestMoreData({bar_count});
          return {{
            before,
            request_more_available:
              typeof series.requestMoreDataAvailable === "function" ? series.requestMoreDataAvailable() : null,
            end_of_data: typeof series.endOfData === "function" ? series.endOfData() : null
          }};
        }})()
        """
    )


def first_candle_epoch(candles: list[dict[str, Any]]) -> float | None:
    timestamps = [candle_epoch_seconds(candle) for candle in candles]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    return min(timestamps) if timestamps else None


def extract_history(
    client: TradingViewClient,
    cutoff_epoch: int,
    end_epoch: int,
    history_attempts: int,
    history_wait_seconds: int,
) -> list[dict[str, Any]]:
    request_visible_history(client, cutoff_epoch, end_epoch)
    time.sleep(history_wait_seconds)
    candles = client.extract_candles_from_active_chart(
        initial_wait_seconds=0,
        retry_wait_seconds=0,
        max_attempts=1,
    )
    for _ in range(history_attempts):
        first = first_candle_epoch(candles)
        if first is not None and first <= cutoff_epoch:
            break
        request_state = request_more_bars(client)
        previous_first = first
        time.sleep(history_wait_seconds)
        candles = client.extract_candles_from_active_chart(
            initial_wait_seconds=0,
            retry_wait_seconds=0,
            max_attempts=1,
        )
        first = first_candle_epoch(candles)
        if first == previous_first and request_state.get("end_of_data") is True:
            break
    return candles


def normalize_candles(
    candles: list[dict[str, Any]],
    job: ExportJob,
    cutoff_epoch: int,
    end_epoch: int,
    exported_at: str,
) -> list[dict[str, Any]]:
    by_timestamp: dict[float, dict[str, Any]] = {}
    for candle in candles:
        timestamp = candle_epoch_seconds(candle)
        if timestamp is None or timestamp < cutoff_epoch or timestamp > end_epoch:
            continue
        if any(candle.get(key) is None for key in ("open", "high", "low", "close")):
            continue
        by_timestamp[timestamp] = {
            "symbol": job.symbol,
            "tradingview_symbol": job.tradingview_symbol,
            "timeframe": job.timeframe,
            "timestamp": iso_timestamp(timestamp),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume": candle.get("volume"),
            "source": "TradingView",
            "exported_at": exported_at,
        }
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def coverage_warning(rows: list[dict[str, Any]], cutoff: datetime) -> str | None:
    if not rows:
        return "no rows were available in the requested range"
    first = datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
    if first > cutoff + timedelta(days=7):
        return f"earliest loaded candle {rows[0]['timestamp']} is later than requested cutoff {cutoff.isoformat()}"
    return None


def export_job(
    client: TradingViewClient,
    job: ExportJob,
    output_dir: Path,
    navigation_timeout: int,
    history_attempts: int,
    history_wait_seconds: int,
) -> tuple[dict[str, Any], str | None]:
    now = datetime.now(UTC)
    cutoff = job.cutoff(now)
    interval = INTERVALS[job.timeframe]
    print(f"\nFetching {job.symbol} | {job.tradingview_symbol} | {job.timeframe}")
    tab = client.open_or_reuse_chart_tab()
    client.navigate_with_cdp(tab, tradingview_url(job.tradingview_symbol, interval), f"export_{job.key}")
    wait_for_chart(client, job.tradingview_symbol, interval, navigation_timeout)
    candles = extract_history(
        client,
        int(cutoff.timestamp()),
        int(now.timestamp()),
        history_attempts,
        history_wait_seconds,
    )
    exported_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    rows = normalize_candles(candles, job, int(cutoff.timestamp()), int(now.timestamp()), exported_at)
    if not rows:
        raise RuntimeError("TradingView returned no raw OHLCV candles in the requested date range")
    output_path = output_dir / job.filename
    write_csv(output_path, rows)
    result = {
        "job": job.key,
        "rows": len(rows),
        "start": rows[0]["timestamp"],
        "end": rows[-1]["timestamp"],
        "path": str(output_path.resolve()),
    }
    print(
        f"Exported {job.symbol} {job.timeframe}: rows={result['rows']}, "
        f"start={result['start']}, end={result['end']}, path={result['path']}"
    )
    return result, coverage_warning(rows, cutoff)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222, help="Existing TradingView CDP debug port")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SYMBOL_TIMEFRAME",
        help="Export only selected jobs, for example --only NIFTY_1H (repeatable)",
    )
    parser.add_argument("--navigation-timeout", type=int, default=45)
    parser.add_argument("--history-attempts", type=int, default=6)
    parser.add_argument("--history-wait-seconds", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = {value.strip().upper() for value in args.only}
    jobs = [job for job in EXPORT_JOBS if not selected or job.key in selected]
    unknown = sorted(selected - {job.key for job in EXPORT_JOBS})
    if unknown:
        print(f"Unknown --only job(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    client = TradingViewClient(args.port)
    try:
        client.connect_to_debug_port()
    except Exception as exc:
        print(f"Cannot connect to existing TradingView CDP port {args.port}: {exc}", file=sys.stderr)
        return 1

    results = []
    failures = []
    warnings = []
    for job in jobs:
        try:
            result, warning = export_job(
                client,
                job,
                args.output_dir,
                args.navigation_timeout,
                args.history_attempts,
                args.history_wait_seconds,
            )
            results.append(result)
            if warning:
                warnings.append({"job": job.key, "warning": warning})
        except Exception as exc:
            failures.append({"job": job.key, "error": str(exc)})
            print(f"FAILED {job.symbol} {job.timeframe}: {exc}", file=sys.stderr)

    print("\nExport summary")
    for result in results:
        print(
            f"OK {result['job']}: rows={result['rows']}, "
            f"start={result['start']}, end={result['end']}, path={result['path']}"
        )
    if warnings:
        print("\nCoverage warnings")
        for warning in warnings:
            print(f"WARNING {warning['job']}: {warning['warning']}")
    print("\nFailures")
    if failures:
        for failure in failures:
            print(f"FAILED {failure['job']}: {failure['error']}")
    else:
        print("None")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
