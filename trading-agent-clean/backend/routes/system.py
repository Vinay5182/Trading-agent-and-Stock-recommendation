from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

from fastapi import APIRouter


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


@router.get("/runtime-info")
async def get_runtime_info() -> dict:
    return {
        "project_root": str(PROJECT_ROOT),
        "backend_pid": os.getpid(),
        "git_commit": _git_commit(),
        "started_at": STARTED_AT,
    }
