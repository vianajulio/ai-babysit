import os
import shutil
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_tool(name: str) -> str:
    override = os.getenv(f"BABYSIT_{name.upper()}_CMD")
    if override:
        return override

    for candidate in (
        _PROJECT_ROOT / ".venv" / "bin" / name,
        _PROJECT_ROOT / "node_modules" / ".bin" / name,
        _PROJECT_ROOT / "tools" / "bin" / name,
    ):
        if candidate.exists():
            return str(candidate)

    return shutil.which(name) or name
