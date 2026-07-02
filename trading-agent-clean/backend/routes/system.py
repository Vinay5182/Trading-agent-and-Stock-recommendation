from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

from fastapi import APIRouter, Request
from config import settings


router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STARTED_AT = datetime.now(timezone.utc).isoformat()


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return "unknown"
    return result.stdout.strip() or "unknown"


import secrets

def get_internal_runtime_info() -> dict:
    """Return raw, unredacted runtime information for internal use."""
    return {
        "project_root": str(PROJECT_ROOT),
        "backend_pid": os.getpid(),
        "git_commit": _git_commit(),
        "started_at": STARTED_AT,
        "automation_disabled": bool(settings.SMOKE_READ_ONLY_MODE),
        "smoke_read_only_mode": bool(settings.SMOKE_READ_ONLY_MODE),
    }


@router.get("/runtime-info")
async def get_runtime_info(request: Request = None) -> dict:
    info = get_internal_runtime_info()
    is_local = False

    # 1. Check env flag
    diagnostics_enabled = os.getenv("TRADING_AGENT_DIAGNOSTICS_ENABLED", "").lower() in ("true", "1", "yes", "on")

    # 2. Check token header
    client_token = None
    if request is not None:
        client_token = request.headers.get("X-Trading-Agent-Diagnostics")

    configured_token = os.getenv("TRADING_AGENT_DIAGNOSTICS_TOKEN")

    # 3. Check loopback address
    is_loopback = False
    if request is not None and request.client is not None:
        client_host = request.client.host
        is_loopback = client_host in ("127.0.0.1", "::1", "localhost", "testclient")
    elif request is None:
        is_loopback = True

    # 4. Verify credentials
    def check_token(a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

    if diagnostics_enabled and is_loopback and configured_token and check_token(client_token, configured_token):
        is_local = True
    elif not configured_token and request is None:
        is_local = True

    if not is_local:
        info["project_root"] = "REDACTED"
        info["backend_pid"] = -1

    return info
