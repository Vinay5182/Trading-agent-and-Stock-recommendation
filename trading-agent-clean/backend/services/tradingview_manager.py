from __future__ import annotations

import asyncio
import logging
import time
import os
import tempfile
import json
import threading
from collections import deque
from datetime import datetime
from typing import Any, Callable

from config import settings

logger = logging.getLogger("uvicorn.error")


class LoopSafeAsyncLock:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._owner_loop_id: int | None = None
        self._guard = threading.RLock()

    def _running_loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _lock_for_loop(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        loop_id = id(loop)
        with self._guard:
            lock = self._locks.get(loop_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[loop_id] = lock
            return lock

    async def acquire(self) -> bool:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        lock = self._lock_for_loop(loop)
        while True:
            with self._guard:
                owner_loop_id = self._owner_loop_id
                if owner_loop_id is None or owner_loop_id == loop_id:
                    break
            await asyncio.sleep(0.01)
        await lock.acquire()
        with self._guard:
            self._owner_loop_id = loop_id
        return True

    def release(self) -> None:
        loop = self._running_loop()
        loop_id = id(loop) if loop is not None else None
        with self._guard:
            candidates = []
            if loop_id is not None and loop_id in self._locks:
                candidates.append((loop_id, self._locks[loop_id]))
            owner_loop_id = self._owner_loop_id
            if owner_loop_id is not None and owner_loop_id in self._locks:
                candidates.append((owner_loop_id, self._locks[owner_loop_id]))
            candidates.extend((candidate_loop_id, lock) for candidate_loop_id, lock in self._locks.items())
            seen = set()
            for candidate_loop_id, lock in candidates:
                if candidate_loop_id in seen:
                    continue
                seen.add(candidate_loop_id)
                if lock.locked():
                    lock.release()
                    if self._owner_loop_id == candidate_loop_id:
                        self._owner_loop_id = None
                    return
        raise RuntimeError("Lock is not acquired.")

    def locked(self) -> bool:
        loop = self._running_loop()
        loop_id = id(loop) if loop is not None else None
        with self._guard:
            if loop_id is not None and loop_id in self._locks and self._locks[loop_id].locked():
                return True
            return self._owner_loop_id is not None or any(lock.locked() for lock in self._locks.values())

    def reset(self) -> None:
        with self._guard:
            for lock in self._locks.values():
                if lock.locked():
                    lock.release()
            self._locks.clear()
            self._owner_loop_id = None


from enum import Enum

class TradingViewState(str, Enum):
    CDP_UNREACHABLE = "CDP_UNREACHABLE"
    NO_VALID_CHART_TAB = "NO_VALID_CHART_TAB"
    SINGLE_TAB_PENDING = "SINGLE_TAB_PENDING"
    MULTIPLE_TABS_SELECTION_REQUIRED = "MULTIPLE_TABS_SELECTION_REQUIRED"
    ATTACHING = "ATTACHING"
    ATTACHED_NOT_READY = "ATTACHED_NOT_READY"
    READY = "READY"
    OPERATION_RUNNING = "OPERATION_RUNNING"
    QUARANTINED = "QUARANTINED"
    ERROR = "ERROR"


class TradingViewPreflightError(Exception):
    def __init__(self, code: str, message: str, details: dict) -> None:
        self.code = code
        self.message = message
        self.details = dict(details)
        if "code" not in self.details:
            self.details["code"] = code
        if "preflight_code" not in self.details:
            self.details["preflight_code"] = code
        if "message" not in self.details:
            self.details["message"] = message
        if "preflight_message" not in self.details:
            self.details["preflight_message"] = message
        if "valid_target_count" not in self.details and "valid_chart_target_count" in self.details:
            self.details["valid_target_count"] = self.details["valid_chart_target_count"]
        super().__init__(message)


class TradingViewExecutionManager:
    def __init__(self, preference_path: str | os.PathLike | None = None) -> None:
        self._lock = LoopSafeAsyncLock()
        self._wait_samples: deque[float] = deque(maxlen=200)
        self._duration_samples: deque[float] = deque(maxlen=200)
        self._queue_length = 0
        self._active_operation: str | None = None
        self._last_operation_name: str | None = None
        self._last_operation_started_at: str | None = None
        self._last_operation_finished_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None
        self._last_error: str | None = None
        self._timeout_count = 0
        self._completed_operation_count = 0
        self._current_tab_id: str | None = None
        self._managed_tab_count = 0
        self._connected = False
        self._attached_target: dict[str, Any] | None = None
        self._cached_preflight = None
        self._preflight_updated_at = 0.0
        self._state_lock = threading.RLock()
        self._operation_generation = 0
        self._active_generation: int | None = None
        self._worker_running_generation: int | None = None
        self._quarantined_generation: int | None = None
        self._quarantined_operation: str | None = None
        self._recovery_started_at: str | None = None
        self._worker_context = threading.local()

        self.preference_file = str(preference_path) if preference_path else os.path.join(os.getcwd(), ".runtime", "tradingview-attachment.json")
        self._preferred_target_id = None
        self._preferred_title = None
        self._preferred_url = None
        self._load_preference()

    def get_client(self, require_attached_tab: bool = False) -> Any:
        from tv_client import TradingViewClient
        return TradingViewClient(
            port=settings.TRADINGVIEW_DEBUG_PORT,
            attached_target_id=self.attached_target_id,
            require_attached_tab=require_attached_tab,
        )

    def _clear_live_attachment(self) -> None:
        self._attached_target = None
        self._current_tab_id = None
        self._connected = False
        self._cached_preflight = None
        self._preflight_updated_at = 0.0

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()

    def _next_generation(self) -> int:
        with self._state_lock:
            self._operation_generation += 1
            return self._operation_generation

    def _recovery_details(self, message: str | None = None) -> dict[str, Any]:
        with self._state_lock:
            operation = self._quarantined_operation or self._active_operation
            generation = self._quarantined_generation
            recovery_started_at = self._recovery_started_at
        return {
            "code": "TV_OPERATION_QUARANTINED",
            "message": message
            or "TradingView manager is recovering; a previous timed-out worker is still finishing.",
            "worker_running": True,
            "recovering_from_timeout": True,
            "quarantined_operation": operation,
            "quarantined_generation": generation,
            "recovery_started_at": recovery_started_at,
            "manager_available": False,
            "retryable": True,
            "attached_target_id": self.attached_target_id,
        }

    def _raise_if_recovering(self) -> None:
        with self._state_lock:
            recovering = self._quarantined_generation is not None
        if recovering:
            details = self._recovery_details()
            raise TradingViewPreflightError("TV_OPERATION_QUARANTINED", details["message"], details)

    def _assert_state_mutation_allowed(self) -> None:
        generation = getattr(self._worker_context, "generation", None)
        if generation is None:
            self._raise_if_recovering()
            return
        with self._state_lock:
            stale = generation != self._active_generation or generation == self._quarantined_generation
        if stale:
            details = self._recovery_details(
                "TradingView worker completion is stale and cannot mutate attachment state."
            )
            raise TradingViewPreflightError("TV_MANAGER_RECOVERING", details["message"], details)

    def _execute_worker(
        self,
        generation: int,
        operation_name: str,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict[str, Any],
    ) -> Any:
        self._worker_context.generation = generation
        self._worker_context.operation_name = operation_name
        try:
            try:
                from tv_client import manager_operation_context
            except Exception:
                return func(*args, **kwargs)
            with manager_operation_context(operation_name, generation):
                return func(*args, **kwargs)
        finally:
            for attr in ("generation", "operation_name"):
                if hasattr(self._worker_context, attr):
                    delattr(self._worker_context, attr)

    def _start_worker_future(
        self,
        operation_name: str,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict[str, Any],
    ) -> tuple[int, asyncio.Future]:
        generation = self._next_generation()
        with self._state_lock:
            self._active_generation = generation
            self._worker_running_generation = generation
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            None,
            self._execute_worker,
            generation,
            operation_name,
            func,
            args,
            kwargs,
        )
        future.add_done_callback(lambda done: self._quarantined_worker_done(generation, operation_name, done))
        return generation, future

    def _mark_worker_done(self, generation: int) -> None:
        with self._state_lock:
            if self._worker_running_generation == generation:
                self._worker_running_generation = None
            if self._active_generation == generation:
                self._active_generation = None

    def _mark_quarantined(self, generation: int, operation_name: str, message: str) -> None:
        now = self._now()
        with self._state_lock:
            self._quarantined_generation = generation
            self._quarantined_operation = operation_name
            self._recovery_started_at = now
            self._active_generation = generation
            self._worker_running_generation = generation
            self._connected = False
            self._last_error_at = now
            self._last_error = message
            self._cached_preflight = {
                "preflight_ready": False,
                "preflight_code": "TV_MANAGER_RECOVERING",
                "preflight_message": message,
                "cdp_reachable": False,
                "valid_chart_target_count": 0,
                "manual_attachment_required": False,
                "attached_target_ready": False,
                "last_attachment_error": message,
            }
            self._preflight_updated_at = time.monotonic()

    def _quarantined_worker_done(
        self,
        generation: int,
        operation_name: str,
        future: asyncio.Future,
    ) -> None:
        with self._state_lock:
            if self._quarantined_generation != generation:
                return
            if self._worker_running_generation == generation:
                self._worker_running_generation = None
            if self._active_generation == generation:
                self._active_generation = None
            if self._active_operation == operation_name:
                self._active_operation = None
            self._quarantined_generation = None
            self._quarantined_operation = None
            self._recovery_started_at = None
            self._cached_preflight = None
            self._preflight_updated_at = 0.0
        try:
            future.exception()
        except (asyncio.CancelledError, Exception):
            pass
        if self._lock.locked():
            self._lock.release()

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

    def _load_preference(self) -> None:
        self._preferred_target_id = None
        self._preferred_title = None
        self._preferred_url = None
        if os.path.exists(self.preference_file):
            try:
                with open(self.preference_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._preferred_target_id = data.get("preferred_target_id")
                    self._preferred_title = data.get("last_known_title")
                    self._preferred_url = data.get("last_known_url")
            except Exception as exc:
                logger.warning("Failed to load TradingView preference: %s", exc)

    def _save_preference(self, attached_snapshot: dict) -> None:
        pref_dir = os.path.dirname(self.preference_file)
        os.makedirs(pref_dir, exist_ok=True)
        data = {
            "preferred_target_id": attached_snapshot.get("target_id"),
            "last_known_title": attached_snapshot.get("title"),
            "last_known_url": attached_snapshot.get("url"),
            "saved_at": self._now(),
        }
        fd, temp_path = tempfile.mkstemp(dir=pref_dir, prefix="tv_attachment_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.preference_file)
            self._preferred_target_id = data["preferred_target_id"]
            self._preferred_title = data["last_known_title"]
            self._preferred_url = data["last_known_url"]
        except Exception as exc:
            logger.warning("Failed to save TradingView preference atomically: %s", exc)
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    async def validate_preference_on_restart(self) -> None:
        self._load_preference()
        if not self._preferred_target_id:
            return
        try:
            await self.run_sync("tv.validate_restart_preference", self._validate_restart_preference_sync)
        except Exception as exc:
            logger.warning("Failed to validate preferred TradingView target on restart: %s", exc)

    def _validate_restart_preference_sync(self) -> None:
        pref_id = self._preferred_target_id
        if not pref_id:
            return
        from tv_client import is_real_tradingview_chart_target, attachable_target_payload
        client = self.get_client()
        try:
            client.connect_to_debug_port()
            tabs = client.list_tabs()
        except Exception:
            self._clear_live_attachment()
            return
        valid_charts = [t for t in tabs if is_real_tradingview_chart_target(t)]
        target_tab = next((t for t in valid_charts if t.get("id") == pref_id), None)
        if not target_tab:
            self._clear_live_attachment()
            return
        try:
            readiness = client.read_chart_readiness(target_tab)
            if readiness.get("activeChartAvailable"):
                target_payload = attachable_target_payload(target_tab, readiness)
                self._attached_target = {
                    "target_id": str(target_payload.get("target_id")),
                    "title": target_payload.get("title"),
                    "url": target_payload.get("url"),
                    "websocket_debugger_url": target_payload.get("websocket_debugger_url"),
                    "ready": True,
                    "chart_readiness": readiness,
                    "attached_at": self._now(),
                }
                self._current_tab_id = str(target_payload.get("target_id"))
                self._connected = True
            else:
                self._clear_live_attachment()
        except Exception:
            self._clear_live_attachment()

    def ensure_ready_attached_target(self, allow_single_tab_auto_attach: bool = True) -> None:
        from tv_client import is_real_tradingview_chart_target, attachable_target_payload
        client = self.get_client()
        cdp_reachable = False
        tabs = []
        try:
            client.connect_to_debug_port()
            cdp_reachable = True
            tabs = client.list_tabs()
        except Exception as exc:
            details = {
                "state": "CDP_UNREACHABLE",
                "cdp_reachable": False,
                "valid_chart_target_count": 0,
                "attached_target_id": None,
                "attached_target_title": None,
                "preflight_ready": False,
                "preflight_code": "TV_CDP_UNREACHABLE",
                "manual_attachment_required": True,
                "operation_allowed": False,
                "operation_running": False,
                "retryable": True,
                "message": f"TradingView CDP port {settings.TRADINGVIEW_DEBUG_PORT} is unreachable: {str(exc)}",
                "sanitized_error": str(exc),
            }
            raise TradingViewPreflightError("TV_CDP_UNREACHABLE", details["message"], details)

        open_charts = [t for t in tabs if is_real_tradingview_chart_target(t)]
        valid_target_count = len(open_charts)
        attached_id = self.attached_target_id
        attached_tab = None

        if attached_id:
            attached_tab = next((t for t in open_charts if str(t.get("id")) == str(attached_id)), None)
            if not attached_tab:
                # Stale preference target, clear it
                self._clear_live_attachment()
                attached_id = None

        # Resolve attachment policy
        if not attached_id:
            if valid_target_count == 0:
                details = {
                    "state": "NO_VALID_CHART_TAB",
                    "cdp_reachable": True,
                    "valid_chart_target_count": 0,
                    "attached_target_id": None,
                    "attached_target_title": None,
                    "preflight_ready": False,
                    "preflight_code": "TV_NO_VALID_CHART_TAB",
                    "manual_attachment_required": True,
                    "operation_allowed": False,
                    "operation_running": False,
                    "retryable": False,
                    "message": "No open TradingView chart tabs found in TradingView Desktop",
                    "sanitized_error": None,
                }
                raise TradingViewPreflightError("TV_NO_VALID_CHART_TAB", details["message"], details)
            elif valid_target_count == 1:
                if not allow_single_tab_auto_attach:
                    details = {
                        "state": "SINGLE_TAB_PENDING",
                        "cdp_reachable": True,
                        "valid_chart_target_count": 1,
                        "attached_target_id": None,
                        "attached_target_title": None,
                        "preflight_ready": False,
                        "preflight_code": "TV_TAB_NOT_ATTACHED",
                        "manual_attachment_required": False,
                        "operation_allowed": True,
                        "operation_running": False,
                        "retryable": False,
                        "message": "One TradingView tab is open and will be auto-attached on run",
                        "sanitized_error": None,
                    }
                    raise TradingViewPreflightError("TV_TAB_NOT_ATTACHED", details["message"], details)

                # Exactly one valid tab: auto-attach!
                tab_to_attach = open_charts[0]
                try:
                    readiness = client.read_chart_readiness(tab_to_attach)
                except Exception as exc:
                    details = {
                        "state": "ERROR",
                        "cdp_reachable": True,
                        "valid_chart_target_count": 1,
                        "attached_target_id": None,
                        "attached_target_title": tab_to_attach.get("title"),
                        "preflight_ready": False,
                        "preflight_code": "TV_ATTACH_FAILED",
                        "manual_attachment_required": False,
                        "operation_allowed": False,
                        "operation_running": False,
                        "retryable": True,
                        "message": f"Failed to connect to chart tab WebSocket: {str(exc)}",
                        "sanitized_error": str(exc),
                    }
                    raise TradingViewPreflightError("TV_ATTACH_FAILED", details["message"], details)

                if readiness.get("activeChartAvailable"):
                    target_payload = attachable_target_payload(tab_to_attach, readiness)
                    attached_snapshot = self.attach_target(target_payload)
                    attached_id = self.attached_target_id
                    attached_tab = tab_to_attach
                else:
                    details = {
                        "state": "ATTACHED_NOT_READY",
                        "cdp_reachable": True,
                        "valid_chart_target_count": 1,
                        "attached_target_id": None,
                        "attached_target_title": tab_to_attach.get("title"),
                        "preflight_ready": False,
                        "preflight_code": "TV_CHART_NOT_READY",
                        "manual_attachment_required": False,
                        "operation_allowed": False,
                        "operation_running": False,
                        "retryable": True,
                        "message": "TradingView tab found but chart API is not ready",
                        "sanitized_error": None,
                    }
                    raise TradingViewPreflightError("TV_CHART_NOT_READY", details["message"], details)
            else:
                # Multiple tabs and no valid selection: fail with selection required
                details = {
                    "state": "MULTIPLE_TABS_SELECTION_REQUIRED",
                    "cdp_reachable": True,
                    "valid_chart_target_count": valid_target_count,
                    "attached_target_id": None,
                    "attached_target_title": None,
                    "preflight_ready": False,
                    "preflight_code": "TV_MULTIPLE_TABS_SELECTION_REQUIRED",
                    "manual_attachment_required": True,
                    "operation_allowed": False,
                    "operation_running": False,
                    "retryable": False,
                    "message": f"Multiple TradingView chart tabs ({valid_target_count}) are open. Please select one in Settings.",
                    "sanitized_error": None,
                }
                raise TradingViewPreflightError("TV_MULTIPLE_TABS_SELECTION_REQUIRED", details["message"], details)

        # Now attached target exists: verify it's still alive and activeChart is ready
        try:
            readiness = client.read_chart_readiness(attached_tab)
        except Exception as exc:
            details = {
                "state": "ERROR",
                "cdp_reachable": True,
                "valid_chart_target_count": valid_target_count,
                "attached_target_id": attached_id,
                "attached_target_title": attached_tab.get("title") if attached_tab else None,
                "preflight_ready": False,
                "preflight_code": "TV_ATTACH_FAILED",
                "manual_attachment_required": False,
                "operation_allowed": False,
                "operation_running": False,
                "retryable": True,
                "message": f"Failed to connect to attached tab WebSocket: {str(exc)}",
                "sanitized_error": str(exc),
            }
            self._clear_live_attachment()
            raise TradingViewPreflightError("TV_ATTACH_FAILED", details["message"], details)

        if not readiness.get("activeChartAvailable"):
            details = {
                "state": "ATTACHED_NOT_READY",
                "cdp_reachable": True,
                "valid_chart_target_count": valid_target_count,
                "attached_target_id": attached_id,
                "attached_target_title": attached_tab.get("title") if attached_tab else None,
                "preflight_ready": False,
                "preflight_code": "TV_CHART_NOT_READY",
                "manual_attachment_required": False,
                "operation_allowed": False,
                "operation_running": False,
                "retryable": True,
                "message": "Attached TradingView tab is active but activeChart is not available",
                "sanitized_error": None,
            }
            raise TradingViewPreflightError("TV_CHART_NOT_READY", details["message"], details)

        self._connected = True
        self._attached_target["ready"] = True
        self._attached_target["chart_readiness"] = readiness

    def get_preflight_status(self) -> dict:
        with self._state_lock:
            recovering = self._quarantined_generation is not None
            active_op = self._active_operation
            lock_held = self._lock.locked()

        op_status = self.operation_status_snapshot()
        queue_depth = op_status.get("queue_length", 0)

        # 1. Recovering/Quarantined State
        if recovering:
            rec_details = self._recovery_details()
            return {
                "state": "QUARANTINED",
                "cdp_reachable": False,
                "valid_chart_target_count": 0,
                "attached_target_id": None,
                "attached_target_title": None,
                "preflight_ready": False,
                "preflight_code": "TV_OPERATION_QUARANTINED",
                "manual_attachment_required": False,
                "operation_allowed": False,
                "operation_running": False,
                "queue_depth": queue_depth,
                "lock_held": lock_held,
                "quarantined": True,
                "retryable": False,
                "message": rec_details["message"],
                "sanitized_error": self._last_error,
                "last_operation": self._last_operation_name,
                "last_operation_started_at": self._last_operation_started_at,
                "last_operation_finished_at": self._last_operation_finished_at,
                # Fallback aliases
                "code": "TV_OPERATION_QUARANTINED",
                "valid_target_count": 0,
                "preflight_message": rec_details["message"],
                "attached_title": None,
                "attached_url": None,
                "attached_target_ready": False,
                "chart_ready": False,
                "last_attachment_error": self._last_error,
            }

        # 2. Busy State
        if lock_held and active_op:
            attached_snapshot = self.attached_target_snapshot()
            attached_id = attached_snapshot.get("target_id") if attached_snapshot else None
            attached_title = attached_snapshot.get("title") if attached_snapshot else None
            attached_url = attached_snapshot.get("url") if attached_snapshot else None
            return {
                "state": "OPERATION_RUNNING",
                "cdp_reachable": True,
                "valid_chart_target_count": 1 if attached_id else 0,
                "attached_target_id": attached_id,
                "attached_target_title": attached_title,
                "preflight_ready": True if attached_id else False,
                "preflight_code": "TV_OPERATION_BUSY",
                "manual_attachment_required": False,
                "operation_allowed": False,
                "operation_running": True,
                "queue_depth": queue_depth,
                "lock_held": True,
                "quarantined": False,
                "retryable": False,
                "message": "TradingView Execution Manager is busy with another operation",
                "sanitized_error": self._last_error,
                "last_operation": self._last_operation_name,
                "last_operation_started_at": self._last_operation_started_at,
                "last_operation_finished_at": self._last_operation_finished_at,
                # Fallback aliases
                "code": "TV_OPERATION_BUSY",
                "valid_target_count": 1 if attached_id else 0,
                "preflight_message": "TradingView Execution Manager is busy with another operation",
                "attached_title": attached_title,
                "attached_url": attached_url,
                "attached_target_ready": True if attached_id else False,
                "chart_ready": True if attached_id else False,
                "last_attachment_error": self._last_error,
            }

        now_mono = time.monotonic()
        if (
            not hasattr(self, "_cached_preflight")
            or self._cached_preflight is None
            or not hasattr(self, "_preflight_updated_at")
            or (now_mono - self._preflight_updated_at) > 10.0
        ):
            from tv_client import is_real_tradingview_chart_target
            client = self.get_client()
            cdp_reachable = False
            valid_chart_target_count = 0
            attached_target_ready = False
            attached_title = self._attached_target.get("title") if self._attached_target else None
            attached_url = self._attached_target.get("url") if self._attached_target else None
            preflight_code = "TV_TAB_NOT_ATTACHED"
            preflight_message = "No TradingView tab attached"
            manual_attachment_required = False
            sanitized_error = None
            state = "NO_VALID_CHART_TAB"

            try:
                client.connect_to_debug_port()
                cdp_reachable = True
                tabs = client.list_tabs()
                open_charts = [t for t in tabs if is_real_tradingview_chart_target(t)]

                ready_charts = []
                for t in open_charts:
                    try:
                        readiness = client.read_chart_readiness(t)
                        if readiness.get("activeChartAvailable"):
                            ready_charts.append((t, readiness))
                    except Exception:
                        pass

                valid_chart_target_count = len(ready_charts)
                attached_id = self.attached_target_id
                attached_title = self._attached_target.get("title") if self._attached_target else None
                attached_url = self._attached_target.get("url") if self._attached_target else None

                if attached_id:
                    attached_tab = next((t for t in open_charts if str(t.get("id")) == str(attached_id)), None)
                    if attached_tab:
                        try:
                            readiness = client.read_chart_readiness(attached_tab)
                            if readiness.get("activeChartAvailable"):
                                attached_target_ready = True
                                preflight_code = "OK"
                                preflight_message = "TradingView is ready"
                                state = "READY"
                            else:
                                preflight_code = "TV_CHART_NOT_READY"
                                preflight_message = "Attached TradingView tab is active but activeChart is not available"
                                state = "ATTACHED_NOT_READY"
                        except Exception as exc:
                            preflight_code = "TV_ATTACH_FAILED"
                            preflight_message = f"Failed to connect to attached tab WebSocket: {str(exc)}"
                            sanitized_error = str(exc)
                            state = "ERROR"
                            self._clear_live_attachment()
                    else:
                        preflight_code = "TV_NO_VALID_CHART_TAB"
                        preflight_message = "Attached tab is no longer open"
                        state = "NO_VALID_CHART_TAB"
                        self._clear_live_attachment()
                else:
                    if valid_chart_target_count == 1:
                        preflight_code = "TV_TAB_NOT_ATTACHED"
                        preflight_message = "One TradingView tab is open and will be auto-attached on run"
                        state = "SINGLE_TAB_PENDING"
                        manual_attachment_required = False
                    elif len(open_charts) == 0:
                        preflight_code = "TV_NO_VALID_CHART_TAB"
                        preflight_message = "No TradingView chart tabs open"
                        state = "NO_VALID_CHART_TAB"
                        manual_attachment_required = True
                    elif valid_chart_target_count == 0:
                        preflight_code = "TV_CHART_NOT_READY"
                        preflight_message = "TradingView tab found but chart API is not ready"
                        state = "ATTACHED_NOT_READY"
                    elif valid_chart_target_count > 1:
                        preflight_code = "TV_MULTIPLE_TABS_SELECTION_REQUIRED"
                        preflight_message = f"Multiple TradingView tabs open ({valid_chart_target_count}). Manual selection required."
                        state = "MULTIPLE_TABS_SELECTION_REQUIRED"
                        manual_attachment_required = True
            except Exception as exc:
                cdp_reachable = False
                preflight_code = "TV_CDP_UNREACHABLE"
                preflight_message = f"TradingView CDP port {settings.TRADINGVIEW_DEBUG_PORT} is unreachable: {str(exc)}"
                sanitized_error = str(exc)
                state = "CDP_UNREACHABLE"
                manual_attachment_required = True

            preflight_ready = (preflight_code == "OK")
            operation_allowed = (state in ("READY", "SINGLE_TAB_PENDING"))

            self._cached_preflight = {
                "state": state,
                "cdp_reachable": cdp_reachable,
                "valid_chart_target_count": valid_chart_target_count,
                "attached_target_id": self.attached_target_id,
                "attached_target_title": attached_title,
                "preflight_ready": preflight_ready,
                "preflight_code": preflight_code,
                "manual_attachment_required": manual_attachment_required,
                "operation_allowed": operation_allowed,
                "operation_running": False,
                "queue_depth": queue_depth,
                "lock_held": lock_held,
                "quarantined": False,
                "retryable": preflight_code in ("TV_CDP_UNREACHABLE", "TV_ATTACH_FAILED", "TV_CHART_NOT_READY"),
                "message": preflight_message,
                "sanitized_error": sanitized_error,
                "last_operation": self._last_operation_name,
                "last_operation_started_at": self._last_operation_started_at,
                "last_operation_finished_at": self._last_operation_finished_at,
                # Fallback aliases
                "code": preflight_code,
                "valid_target_count": valid_chart_target_count,
                "preflight_message": preflight_message,
                "attached_title": attached_title,
                "attached_url": attached_url,
                "attached_target_ready": attached_target_ready,
                "chart_ready": attached_target_ready,
                "last_attachment_error": self._last_error,
            }
            self._preflight_updated_at = now_mono

        return dict(self._cached_preflight)

    def attach_target(self, target: dict[str, Any]) -> dict[str, Any]:
        self._assert_state_mutation_allowed()
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
        self._save_preference(self._attached_target)
        self._cached_preflight = None
        self._preflight_updated_at = 0.0
        return dict(self._attached_target)

    def _detach_target_unlocked(self) -> None:
        self._assert_state_mutation_allowed()
        self._attached_target = None
        self._current_tab_id = None
        self._preferred_target_id = None
        self._preferred_title = None
        self._preferred_url = None
        if os.path.exists(self.preference_file):
            try:
                os.remove(self.preference_file)
            except Exception:
                pass
        self._cached_preflight = None
        self._preflight_updated_at = 0.0

    def detach_target(self) -> None:
        self._detach_target_unlocked()

    def _detach_target_sync(self) -> dict[str, Any]:
        previous = self.attached_target_snapshot()
        self._detach_target_unlocked()
        return {
            "attached": False,
            "attached_target": None,
            "attached_target_id": None,
            "detached": previous is not None,
            "previous_attached_target": previous,
        }

    async def detach_target_serialized(self) -> dict[str, Any]:
        return await self.run_sync(
            "tv.detach_tab",
            self._detach_target_sync,
            timeout_seconds=25,
            retries=0,
        )

    def clear_attachment_if_target(self, target_id: str | None) -> None:
        self._raise_if_recovering()
        if target_id and self.attached_target_id == str(target_id):
            self.detach_target()

    def attached_target_snapshot(self) -> dict[str, Any] | None:
        return dict(self._attached_target) if self._attached_target else None

    def operation_status_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            recovering = self._quarantined_generation is not None
            worker_running = self._lock.locked() or self._worker_running_generation is not None
        return {
            "worker_running": worker_running,
            "queue_length": self._queue_length,
            "active_operation": self._active_operation,
            "recovering_from_timeout": recovering,
            "quarantined_operation": self._quarantined_operation,
            "quarantined_generation": self._quarantined_generation,
            "recovery_started_at": self._recovery_started_at,
            "manager_available": not worker_running and not recovering,
        }

    def _busy_details(self, message: str) -> dict[str, Any]:
        with self._state_lock:
            recovering = self._quarantined_generation is not None
        if recovering:
            return self._recovery_details()
        cached = self._cached_preflight or {}
        return {
            "code": "TV_MANAGER_BUSY",
            "message": message,
            "cdp_reachable": cached.get("cdp_reachable", True),
            "valid_chart_target_count": cached.get("valid_chart_target_count", 1 if self.attached_target_id else 0),
            "attached_target_id": self.attached_target_id,
            "attached_title": self._attached_target.get("title") if self._attached_target else None,
            "attached_url": self._attached_target.get("url") if self._attached_target else None,
            "chart_ready": cached.get("attached_target_ready", True),
            "manual_attachment_required": cached.get("manual_attachment_required", False),
            "retryable": True,
        }

    async def run_read_only_inspection(
        self,
        operation_name: str,
        func: Callable[..., Any],
        *args,
        timeout_seconds: int | None = None,
        **kwargs,
    ) -> Any:
        timeout = timeout_seconds or settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS
        self._raise_if_recovering()
        if self._lock.locked():
            message = "TradingView Execution Manager is busy with another operation"
            raise TradingViewPreflightError("TV_MANAGER_BUSY", message, self._busy_details(message))

        lock_acquired = False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=0.05)
            lock_acquired = True
        except asyncio.TimeoutError as exc:
            message = "TradingView Execution Manager read-only inspection could not acquire lock"
            raise TradingViewPreflightError("TV_MANAGER_BUSY", message, self._busy_details(message)) from exc

        release_lock = lock_acquired
        self._active_operation = operation_name
        generation, worker_future = self._start_worker_future(operation_name, func, args, kwargs)
        try:
            result = await asyncio.wait_for(asyncio.shield(worker_future), timeout=timeout)
            self._mark_worker_done(generation)
            return result
        except asyncio.TimeoutError as exc:
            self._timeout_count += 1
            message = f"{operation_name} timed out after {timeout}s"
            self._mark_quarantined(generation, operation_name, message)
            release_lock = False
            raise TimeoutError(message) from exc
        except asyncio.CancelledError:
            if not worker_future.done():
                message = f"{operation_name} was cancelled while its TradingView worker was still running"
                self._mark_quarantined(generation, operation_name, message)
                release_lock = False
            else:
                self._mark_worker_done(generation)
            raise
        except Exception:
            self._mark_worker_done(generation)
            raise
        finally:
            if release_lock:
                self._active_operation = None
                self._lock.release()

    async def run_operation(
        self,
        operation_name: str,
        func: Callable[..., Any],
        *args,
        require_chart: bool = True,
        allow_single_tab_auto_attach: bool = True,
        timeout_seconds: int | None = None,
        queue_timeout_seconds: float = 30.0,
        retries: int = 0,
        **kwargs,
    ) -> Any:
        return await self.run_sync(
            operation_name,
            func,
            *args,
            timeout_seconds=timeout_seconds,
            retries=retries,
            require_preflight=require_chart,
            allow_single_tab_auto_attach=allow_single_tab_auto_attach,
            **kwargs,
        )

    async def run_sync(
        self,
        operation_name: str,
        func: Callable[..., Any],
        *args,
        timeout_seconds: int | None = None,
        retries: int = 0,
        recoverable: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError, OSError),
        require_preflight: bool = False,
        allow_single_tab_auto_attach: bool = True,
        **kwargs,
    ) -> Any:
        timeout = timeout_seconds or settings.TRADINGVIEW_SYMBOL_TIMEOUT_SECONDS
        self._raise_if_recovering()
        queued_at = time.monotonic()
        self._queue_length += 1

        MAX_QUEUE_CAPACITY = 3
        if self._queue_length > MAX_QUEUE_CAPACITY:
            self._queue_length = max(self._queue_length - 1, 0)
            details = self._busy_details("TradingView Execution Manager queue capacity exceeded")
            raise TradingViewPreflightError("TV_OPERATION_BUSY", details["message"], details)
        if require_preflight and self._lock.locked():
            self._queue_length = max(self._queue_length - 1, 0)
            details = self._busy_details("TradingView Execution Manager is busy with another operation")
            raise TradingViewPreflightError("TV_OPERATION_BUSY", details["message"], details)

        lock_acquired = False
        try:
            # Wait at most 30 seconds to acquire the lock
            await asyncio.wait_for(self._lock.acquire(), timeout=30.0)
            lock_acquired = True
        except asyncio.TimeoutError as exc:
            self._queue_length = max(self._queue_length - 1, 0)
            details = self._busy_details("TradingView Execution Manager queue wait timed out")
            raise TradingViewPreflightError("TV_OPERATION_BUSY", details["message"], details) from exc

        self._queue_length = max(self._queue_length - 1, 0)
        wait_ms = (time.monotonic() - queued_at) * 1000
        self._wait_samples.append(wait_ms)
        self._active_operation = operation_name
        self._last_operation_name = operation_name
        self._last_operation_started_at = self._now()
        started_at = time.monotonic()

        duration_recorded = False
        release_lock = lock_acquired
        try:
            if require_preflight:
                self.ensure_ready_attached_target(allow_single_tab_auto_attach=allow_single_tab_auto_attach)

            attempt = 0
            while True:
                generation, worker_future = self._start_worker_future(
                    operation_name,
                    func,
                    args,
                    kwargs,
                )
                try:
                    result = await asyncio.wait_for(asyncio.shield(worker_future), timeout=timeout)
                    self._mark_worker_done(generation)
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
                    self._last_error = f"{operation_name} timed out after {timeout}s"
                    self._mark_quarantined(generation, operation_name, self._last_error)
                    release_lock = False
                    logger.exception("TradingView operation timed out operation=%s timeout=%s", operation_name, timeout)
                    raise TimeoutError(self._last_error) from exc
                except asyncio.CancelledError:
                    if not worker_future.done():
                        message = f"{operation_name} was cancelled while its TradingView worker was still running"
                        self._mark_quarantined(generation, operation_name, message)
                        release_lock = False
                    else:
                        self._mark_worker_done(generation)
                    raise
                except recoverable as exc:
                    self._mark_worker_done(generation)
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
                    self._mark_worker_done(generation)
                    self._connected = False
                    self._last_error_at = self._now()
                    self._last_error = str(exc)
                    logger.exception("TradingView operation failed operation=%s", operation_name)
                    raise
        finally:
            self._last_operation_finished_at = self._now()
            if not duration_recorded:
                duration_ms = (time.monotonic() - started_at) * 1000
                self._duration_samples.append(duration_ms)
            if release_lock:
                self._active_operation = None
                with self._state_lock:
                    if self._active_generation is not None:
                        self._active_generation = None
                self._lock.release()

    def runtime_status(self) -> dict:
        durations = list(self._duration_samples)
        preflight = self.get_preflight_status()
        op_status = self.operation_status_snapshot()
        return {
            "connected": self._connected,
            "worker_running": op_status["worker_running"],
            "queue_length": op_status["queue_length"],
            "active_operation": op_status["active_operation"],
            "recovering_from_timeout": op_status["recovering_from_timeout"],
            "quarantined_operation": op_status["quarantined_operation"],
            "quarantined_generation": op_status["quarantined_generation"],
            "recovery_started_at": op_status["recovery_started_at"],
            "manager_available": op_status["manager_available"],
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
            **preflight
        }

    def reset(self) -> None:
        self._lock.reset()
        self._active_operation = None
        self._queue_length = 0
        self._connected = False
        self._current_tab_id = None
        self._managed_tab_count = 0
        self._attached_target = None
        self._cached_preflight = None
        self._preflight_updated_at = 0.0
        with self._state_lock:
            self._active_generation = None
            self._worker_running_generation = None
            self._quarantined_generation = None
            self._quarantined_operation = None
            self._recovery_started_at = None


tradingview_manager = TradingViewExecutionManager()
