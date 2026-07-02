import asyncio
from datetime import datetime
import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import app
from routes import system


def test_openapi_exposes_system_runtime_info() -> None:
    paths = app.openapi()["paths"]

    assert "/api/system/runtime-info" in paths
    assert "get" in paths["/api/system/runtime-info"]


def test_runtime_info_identifies_current_project_and_process() -> None:
    # Test the internal contract (helper must return actual system values)
    result = system.get_internal_runtime_info()

    assert Path(result["project_root"]).resolve() == Path(__file__).resolve().parents[2]
    assert result["backend_pid"] == os.getpid()
    assert result["git_commit"]
    assert isinstance(result["git_commit"], str)
    datetime.fromisoformat(result["started_at"])
    assert result["automation_disabled"] is False
    assert result["smoke_read_only_mode"] is False


def test_public_api_runtime_info_redacts_sensitive_paths() -> None:
    # Test the public API boundary contract
    from fastapi.testclient import TestClient
    client = TestClient(app)
    response = client.get("/api/system/runtime-info")
    assert response.status_code == 200
    data = response.json()

    assert data["project_root"] == "REDACTED"
    assert data["backend_pid"] == -1

    # Ensure sensitive paths are absolutely not exposed in the serialized response
    text_response = response.text
    for sensitive_keyword in ("C:\\Users\\Asus", "Asus", "OneDrive", "trading-agent-clean"):
        assert sensitive_keyword not in text_response, (
            f"Leakage detected! Public response contains sensitive substring {sensitive_keyword!r}"
        )



def test_runtime_info_reports_smoke_automation_disabled() -> None:
    original = system.settings.SMOKE_READ_ONLY_MODE
    object.__setattr__(system.settings, "SMOKE_READ_ONLY_MODE", True)
    try:
        result = asyncio.run(system.get_runtime_info())
    finally:
        object.__setattr__(system.settings, "SMOKE_READ_ONLY_MODE", original)

    assert result["automation_disabled"] is True
    assert result["smoke_read_only_mode"] is True


def test_runtime_info_sanitizes_public_requests() -> None:
    from fastapi.testclient import TestClient
    client = TestClient(app)
    response = client.get("/api/system/runtime-info")
    assert response.status_code == 200
    data = response.json()
    assert data["project_root"] == "REDACTED"
    assert data["backend_pid"] == -1


def test_runtime_info_proxy_security_enforcement() -> None:
    from fastapi.testclient import TestClient
    client = TestClient(app)

    # 1. Direct loopback request (but without headers / env vars configured) -> redacted
    response = client.get("/api/system/runtime-info")
    assert response.status_code == 200
    assert response.json()["project_root"] == "REDACTED"
    assert response.json()["backend_pid"] == -1

    # 2. Spoof headers like X-Forwarded-For -> redacted
    response = client.get("/api/system/runtime-info", headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.json()["project_root"] == "REDACTED"

    # 3. Enable diagnostics but pass no token -> redacted
    os.environ["TRADING_AGENT_DIAGNOSTICS_ENABLED"] = "true"
    try:
        response = client.get("/api/system/runtime-info")
        assert response.json()["project_root"] == "REDACTED"
    finally:
        os.environ.pop("TRADING_AGENT_DIAGNOSTICS_ENABLED", None)

    # 4. Enable diagnostics and configure a token, but pass incorrect token -> redacted
    os.environ["TRADING_AGENT_DIAGNOSTICS_ENABLED"] = "true"
    os.environ["TRADING_AGENT_DIAGNOSTICS_TOKEN"] = "my-secret-token"
    try:
        response = client.get("/api/system/runtime-info", headers={"X-Trading-Agent-Diagnostics": "wrong-token"})
        assert response.json()["project_root"] == "REDACTED"

        # 5. Correct token -> authorized (returns real PID and path)
        response = client.get("/api/system/runtime-info", headers={"X-Trading-Agent-Diagnostics": "my-secret-token"})
        assert response.json()["project_root"] != "REDACTED"
        assert response.json()["backend_pid"] == os.getpid()

        # 6. Diagnostics-disabled configuration -> always redacted even with correct token
        os.environ["TRADING_AGENT_DIAGNOSTICS_ENABLED"] = "false"
        response = client.get("/api/system/runtime-info", headers={"X-Trading-Agent-Diagnostics": "my-secret-token"})
        assert response.json()["project_root"] == "REDACTED"
    finally:
        os.environ.pop("TRADING_AGENT_DIAGNOSTICS_ENABLED", None)
        os.environ.pop("TRADING_AGENT_DIAGNOSTICS_TOKEN", None)
