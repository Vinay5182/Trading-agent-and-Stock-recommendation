from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

from config import settings


logger = logging.getLogger("uvicorn.error")


class TradingViewExecutionManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._wait_samples: deque[float] = deque(maxlen=200)
        self._duration_samples: deque[float] = deque(maxlen=200)
        self._queue_length = 0
        self._active_operation: str | None = None
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None
        self._last_error: str | None = None
        self._timeout_count = 0
        self._completed_operation_count = 0
        self._current_tab_id: str | None = None
        self._managed_tab_count = 0
        self._connected = False
        self._attached_target: dict[str, Any] | None = None

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()

    def _record_diagnostics(self, value: Any) -> None:
        diagnostics = None
        if isinstance(value, dict):
            diagnostics = value.get("diagnostics") or value.get("chart_status") or value
        if not isinstance(diagnostics, dict):
            return
        tab_id = diagnostics.get("managed_tab_id") or diagnostics.get("current_tab_id") or diagnostics.get("tab_id")
        if tab_id:
            self._current_tab_id = str(tab_id)
        managed_count = diagnostics.get("managed_tab_count")
        if isinstance(managed_count, int):
            self._managed_tab_count = managed_count
        if diagnostics.get("devtools_version_ok") or diagnostics.get("devtools_ws_connected"):
            self._connected = True

    @property
    def attached_target_id(self) -> str | None:
        return self._attached_target.get("target_id") if self._attached_target else None

    def attach_target(self, target: dict[str, Any]) -> dict[str, Any]:
        target_id = target.get("target_id") or target.get("id")
        if not target_id:
            raise ValueError("target_id is required")
        self._attached_target = {
            "target_id": str(target_id),
            "title": target.get("title"),
            "url": target.get("url"),
            "websocket_debugger_url": target.get("websocket_debugger_url") or target.get("webSocketDebuggerUrl"),
            "ready": bool(target.get("ready", True)),
            "chart_readiness": target.get("chart_readiness") or {},
            "attached_at": self._now(),
        }
        self._current_tab_id = str(target_id)
        self._connected = True
        return dict(self._attached_target)

    def detach_target(self) -> None:
        self._attached_target = None
        self._current_tab_id = None

    def clear_attachment_if_target(self, target_id: str | None) -> None:
        if target_id and self.attached_target_id == str(target_id):
            self.detach_target()

    def attached_target_snapshot(self) -> dict[str, Any] | None:
        return dict(self._attached_target) if self._attached_target else None

    async def run_sync(
        self,
        operation_name: str,
        func: Callable[..., Any],
        *args,
        timeout_seconds: int | None = None,
        retries: int = 0,
        recoverable: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError, OSError),
        **kwargs,
    ) -> Any:
        timeout = timeout_seconds or settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS
        queued_at = time.monotonic()
        self._queue_length += 1
        async with self._lock:
            self._queue_length = max(self._queue_length - 1, 0)
            wait_ms = (time.monotonic() - queued_at) * 1000
            self._wait_samples.append(wait_ms)
            self._active_operation = operation_name
            started_at = time.monotonic()
            attempt = 0
            duration_recorded = False
            try:
                while True:
                    try:
                        result = await asyncio.wait_for(
                            asyncio.to_thread(func, *args, **kwargs),
                            timeout=timeout,
                        )
                        duration_ms = (time.monotonic() - started_at) * 1000
                        self._duration_samples.append(duration_ms)
                        duration_recorded = True
                        self._completed_operation_count += 1
                        self._last_success_at = self._now()
                        self._last_error = None
                        self._record_diagnostics(result)
                        return result
                    except asyncio.TimeoutError as exc:
                        self._timeout_count += 1
                        self._connected = False
                        self._last_error_at = self._now()
                        self._last_error = f"{operation_name} timed out after {timeout}s"
                        logger.exception("TradingView operation timed out operation=%s timeout=%s", operation_name, timeout)
                        raise TimeoutError(self._last_error) from exc
                    except recoverable as exc:
                        self._connected = False
                        self._last_error_at = self._now()
                        self._last_error = str(exc)
                        if attempt >= retries:
                            logger.exception("TradingView operation failed operation=%s", operation_name)
                            raise
                        attempt += 1
                        logger.warning(
                            "TradingView recoverable failure operation=%s attempt=%d retrying error=%s",
                            operation_name,
                            attempt,
                            exc,
                        )
                    except Exception as exc:
                        self._connected = False
                        self._last_error_at = self._now()
                        self._last_error = str(exc)
                        logger.exception("TradingView operation failed operation=%s", operation_name)
                        raise
            finally:
                if not duration_recorded:
                    duration_ms = (time.monotonic() - started_at) * 1000
                    self._duration_samples.append(duration_ms)
                self._active_operation = None

    def runtime_status(self) -> dict:
        durations = list(self._duration_samples)
        return {
            "connected": self._connected,
            "worker_running": self._lock.locked(),
            "queue_length": self._queue_length,
            "active_operation": self._active_operation,
            "current_tab_id": self._current_tab_id,
            "managed_tab_count": self._managed_tab_count,
            "attached_target_id": self.attached_target_id,
            "attached_target": self.attached_target_snapshot(),
            "last_success_at": self._last_success_at,
            "last_error_at": self._last_error_at,
            "last_error": self._last_error,
            "timeout_count": self._timeout_count,
            "completed_operation_count": self._completed_operation_count,
            "average_operation_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0,
            "average_queue_wait_ms": round(sum(self._wait_samples) / len(self._wait_samples), 2) if self._wait_samples else 0,
            "max_cdp_concurrency": 1,
        }

    def reset(self) -> None:
        self._active_operation = None
        self._queue_length = 0
        self._connected = False
        self._current_tab_id = None
        self._managed_tab_count = 0
        self._attached_target = None


tradingview_manager = TradingViewExecutionManager()
