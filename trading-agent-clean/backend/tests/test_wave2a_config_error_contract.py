import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import database
from main import app
from services.system_errors import record_system_error


class FakeSystemErrors:
    def __init__(self) -> None:
        self.rows = []

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        row = next((candidate for candidate in self.rows if all(candidate.get(key) == value for key, value in query.items())), None)
        if row is None:
            row = {**query, **update.get("$setOnInsert", {})}
            self.rows.append(row)
        row.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        return SimpleNamespace(modified_count=1, matched_count=1, upserted_id=None)


def test_invalid_config_is_deterministic_and_redaction_safe(monkeypatch):
    monkeypatch.setenv("BACKEND_PORT", "not-a-port")
    with pytest.raises(config.ConfigValidationError) as exc_info:
        config.Settings()
    assert exc_info.value.code == "CONFIG_INVALID_INTEGER"
    assert exc_info.value.details == {"field": "BACKEND_PORT"}

    invalid_uri = replace(config.settings, MONGO_URI="not-a-uri")
    with pytest.raises(config.ConfigValidationError) as uri_exc:
        config.validate_settings(invalid_uri)
    assert uri_exc.value.code == "CONFIG_INVALID_MONGO_URI"
    assert "not-a-uri" not in uri_exc.value.message


def test_smoke_and_normal_startup_validation_modes():
    smoke = replace(config.settings, SMOKE_READ_ONLY_MODE=True)
    normal = replace(config.settings, SMOKE_READ_ONLY_MODE=False, PAPER_UPDATE_SCHEDULER_MODE="dry_run_only")

    assert config.validate_settings(smoke) == {"ok": True, "automation_disabled": True, "smoke_read_only_mode": True}
    assert config.validate_settings(normal) == {"ok": True, "automation_disabled": False, "smoke_read_only_mode": False}


def test_unsafe_scheduler_config_fails_before_startup():
    unsafe = replace(
        config.settings,
        SMOKE_READ_ONLY_MODE=False,
        PAPER_UPDATE_SCHEDULER_MODE="real",
        PAPER_UPDATE_SCHEDULER_ALLOW_REAL_WRITES=False,
    )
    with pytest.raises(config.ConfigValidationError) as exc_info:
        config.validate_settings(unsafe)
    assert exc_info.value.code == "CONFIG_UNSAFE_SCHEDULER"


def test_lifespan_smoke_skips_automation_and_normal_preserves_startup(monkeypatch):
    from services import mongo_indexes, paper_automation
    from services.tradingview_manager import tradingview_manager

    calls = []

    class FakeClient:
        def __getitem__(self, name):
            calls.append(("db", name))
            return SimpleNamespace()

        def close(self):
            calls.append(("close", None))

    async def fake_indexes(db):
        calls.append(("indexes", db))
        return {"critical_expected": 0, "critical_verified": [], "critical_created": []}

    async def fake_initialize(db):
        calls.append(("initialize", db))

    async def fake_validate_preference():
        calls.append(("validate_preference", None))

    def fake_start():
        calls.append(("start_automation", None))
        return SimpleNamespace(get_name=lambda: "fake-paper-automation")

    async def fake_shutdown():
        calls.append(("shutdown", None))

    async def run_lifespan(is_smoke: bool):
        calls.clear()
        monkeypatch.setattr(database, "settings", replace(config.settings, SMOKE_READ_ONLY_MODE=is_smoke))
        monkeypatch.setattr(database, "AsyncIOMotorClient", lambda uri: FakeClient())
        monkeypatch.setattr(mongo_indexes, "ensure_active_indexes", fake_indexes)
        monkeypatch.setattr(paper_automation, "initialize_scheduler_status", fake_initialize)
        monkeypatch.setattr(tradingview_manager, "validate_preference_on_restart", fake_validate_preference)
        monkeypatch.setattr(paper_automation, "start_paper_automation_once", fake_start)
        monkeypatch.setattr(paper_automation, "shutdown_paper_automation", fake_shutdown)
        async with database.lifespan(SimpleNamespace()):
            calls.append(("yielded", None))
        return list(calls)

    smoke_calls = asyncio.run(run_lifespan(True))
    normal_calls = asyncio.run(run_lifespan(False))

    assert [name for name, _ in smoke_calls] == ["db", "indexes", "yielded", "close"]
    assert [name for name, _ in normal_calls] == [
        "db",
        "indexes",
        "db",
        "initialize",
        "validate_preference",
        "start_automation",
        "yielded",
        "shutdown",
        "close",
    ]


def test_status_script_uses_shared_port_parameters():
    root = Path(__file__).resolve().parents[2]
    status_script = (root / "status-trading-agent.ps1").read_text(encoding="utf-8")
    process_module = (root / "scripts" / "TradingAgent.Processes.psm1").read_text(encoding="utf-8")

    assert "param(" in status_script
    assert "Get-TradingAgentStatus -BackendPort $BackendPort -FrontendPort $FrontendPort" in status_script
    assert "Get-PortOwners $BackendPort" in status_script
    assert "Get-PortOwners $FrontendPort" in status_script
    assert "Test-PortListening $MongoPort" in status_script
    assert "Test-PortListening $TradingViewPort" in status_script
    assert "param(\n        [int]$BackendPort = 8011," in process_module
    assert "Test-BackendHealth -Port $BackendPort" in process_module
    assert "Get-BackendRuntimeRoot -BackendPort $BackendPort" in process_module


def test_http_exception_contract_redacts_secret_and_path():
    route = "/__test/wave2a/http-error"

    @app.get(route)
    async def _wave2a_http_error():
        raise HTTPException(
            status_code=400,
            detail={
                "code": "TEST_BAD_INPUT",
                "message": r"bad token=abc123 at C:\Users\Asus\secret.txt",
                "details": {"path": r"C:\tmp\secret.txt", "url": "https://x.test?api_key=abc123"},
            },
        )

    response = TestClient(app, raise_server_exceptions=False).get(route)

    assert response.status_code == 400
    body = response.json()
    assert set(body) >= {"code", "message", "details"}
    assert body["code"] == "TEST_BAD_INPUT"
    assert "abc123" not in str(body)
    assert "Users" not in str(body)
    assert "<redacted-path>" in str(body)
    assert "api_key=<redacted>" in str(body)


def test_unhandled_exception_contract_is_generic_and_redacted():
    route = "/__test/wave2a/unhandled"

    @app.get(route)
    async def _wave2a_unhandled():
        raise RuntimeError(r"boom password=abc123 C:\Users\Asus\secret.txt")

    response = TestClient(app, raise_server_exceptions=False).get(route)

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_SERVER_ERROR"
    assert "abc123" not in str(body)
    assert "Users" not in str(body)
    assert body["details"] == {"exception_type": "RuntimeError"}


def test_system_error_persistence_redacts_message_and_context():
    collection = FakeSystemErrors()
    db = SimpleNamespace(system_errors=collection)

    result = asyncio.run(
        record_system_error(
            db,
            component="provider",
            operation="fetch",
            exception=RuntimeError(r"failed token=abc123 C:\Users\Asus\secret.txt"),
            context={"url": "https://x.test?access_token=abc123", "path": r"C:\tmp\secret.txt"},
        )
    )

    assert result["recorded"] is True
    row = collection.rows[0]
    assert "abc123" not in str(row)
    assert "Users" not in str(row)
    assert row["context"]["url"].endswith("access_token=<redacted>")
    assert row["context"]["path"] == "<redacted-path>"
