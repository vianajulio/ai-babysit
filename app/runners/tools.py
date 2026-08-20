import os
import shutil
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_tool(name: str) -> str:
    override = os.getenv(f"BABYSIT_{name.upper()}_CMD")
    if override:
        return override

    candidates = [
        _PROJECT_ROOT / ".venv" / "bin" / name,
        _PROJECT_ROOT / ".venv" / "Scripts" / name,
        _PROJECT_ROOT / "node_modules" / ".bin" / name,
        _PROJECT_ROOT / "tools" / "bin" / name,
    ]
    if os.name == "nt":
        # npm/POSIX shims without an extension are shell scripts that Windows
        # cannot spawn; prefer the native .cmd/.exe shim first.
        windows_variants = []
        for base in candidates:
            windows_variants.extend(
                candidate for candidate in (
                    base.with_name(base.name + ".cmd"),
                    base.with_name(base.name + ".exe"),
                    base,
                )
            )
        for candidate in windows_variants:
            if candidate.exists():
                return str(candidate)
    else:
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

    return shutil.which(name) or name
