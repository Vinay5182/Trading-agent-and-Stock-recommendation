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
    result = asyncio.run(system.get_runtime_info())

    assert Path(result["project_root"]).resolve() == Path(__file__).resolve().parents[2]
    assert result["backend_pid"] == os.getpid()
    assert result["git_commit"]
    assert isinstance(result["git_commit"], str)
    datetime.fromisoformat(result["started_at"])
