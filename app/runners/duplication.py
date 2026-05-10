import asyncio
import json
from pathlib import Path

from app.gates.models import CheckResult, GateStatus, Violation
from app.runners.tools import resolve_tool


class DuplicationRunner:
    name = "duplication"

    def __init__(self, max_percent: float = 5.0, fail_only_on_changed_files: bool = False):
        self.max_percent = max_percent
        self.fail_only_on_changed_files = fail_only_on_changed_files

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        report_path = workspace.parent / "jscpd-report.json"
        cmd = [
            resolve_tool("jscpd"), str(workspace),
            "--reporters", "json",
            "--output", str(workspace.parent),
            "--silent",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return CheckResult(check=self.name, status=GateStatus.error,
                               metrics={"error": "jscpd timeout"})
        except FileNotFoundError:
            return CheckResult(check=self.name, status=GateStatus.skipped,
                               metrics={"error": "jscpd não instalado"})

        if not report_path.exists():
            return CheckResult(check=self.name, status=GateStatus.skipped,
                               metrics={"error": "jscpd não gerou relatório"})

        try:
            report = json.loads(report_path.read_text())
        except (json.JSONDecodeError, OSError):
            return CheckResult(check=self.name, status=GateStatus.error,
                               metrics={"error": "relatório jscpd inválido"})

        stats = report.get("statistics", {}).get("total", {})
        percentage = stats.get("percentage", 0.0)
        clones = report.get("duplicates", [])

        changed_set = {f.lstrip("/") for f in changed_files}
        violations = []
        for clone in clones:
            for fragment in clone.get("duplicationA", []) + clone.get("duplicationB", []):
                file_path = fragment.get("sourceId", "")
                rel = self._to_rel(file_path, workspace)
                if rel in changed_set:
                    violations.append(Violation(
                        file=rel,
                        line=fragment.get("start", {}).get("line"),
                        severity="medium",
                        message="Bloco duplicado detectado",
                        current_value=percentage,
                        allowed_value=self.max_percent,
                    ))
                    break

        threshold_exceeded = percentage > self.max_percent
        if self.fail_only_on_changed_files:
            status = GateStatus.failed if threshold_exceeded and violations else GateStatus.passed
        else:
            status = GateStatus.failed if threshold_exceeded else GateStatus.passed

        return CheckResult(
            check=self.name,
            status=status,
            metrics={"duplication_percent": round(percentage, 2), "clone_count": len(clones)},
            violations=violations,
        )

    @staticmethod
    def _to_rel(path: str, workspace: Path) -> str:
        try:
            return str(Path(path).relative_to(workspace))
        except ValueError:
            return path
