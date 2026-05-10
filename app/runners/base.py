from pathlib import Path
from typing import Protocol

from app.gates.models import CheckResult


class Runner(Protocol):
    name: str

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult: ...
