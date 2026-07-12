# Root-level pytest conftest.
#
# Redirects pytest's tmp_path basetemp to a project-local directory
# (backend/pytest_temp/) to avoid the Windows ACL PermissionError that
# occurs when pytest tries to scan/create AppData/Local/Temp/pytest-of-Asus
#
# backend/pytest_temp/ is already covered by .gitignore.

from pathlib import Path


def pytest_configure(config) -> None:
    """Redirect tmp_path basetemp to a project-local writable directory."""
    project_root = Path(__file__).parent
    basetemp = project_root / "backend" / "pytest_temp"
    basetemp.mkdir(parents=True, exist_ok=True)
    # Only set if the user hasn't already specified --basetemp
    if not config.option.__dict__.get("basetemp"):
        config.option.basetemp = str(basetemp)
