import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from app.gates.models import CheckResult, GateStatus, Violation
from app.runners.tools import resolve_tool


class SecretsRunner:
    name = "secrets"

    def __init__(self, report_dir: Path | None = None):
        self.report_dir = report_dir

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        owns_report_dir = self.report_dir is None
        report_dir = self.report_dir or Path(
            tempfile.mkdtemp(prefix="gitleaks-", dir=str(workspace.parent))
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "gitleaks-report.json"

        try:
            cmd = [
                resolve_tool("gitleaks"), "detect",
                "--source", str(workspace),
                "--report-path", str(report_path),
                "--report-format", "json",
                "--no-git",
                "--exit-code", "0",
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                return CheckResult(check=self.name, status=GateStatus.error,
                                   metrics={"error": "gitleaks timeout"})
            except FileNotFoundError:
                return CheckResult(check=self.name, status=GateStatus.skipped,
                                   metrics={"error": "gitleaks não instalado"})

            if not report_path.exists():
                return CheckResult(check=self.name, status=GateStatus.passed, metrics={"secrets_found": 0})

            try:
                findings = json.loads(report_path.read_text())
            except (json.JSONDecodeError, OSError):
                findings = []

            if not isinstance(findings, list):
                findings = []

            changed_set = {f.lstrip("/") for f in changed_files}
            violations = []
            for finding in findings:
                file_path = finding.get("File", "")
                rel = self._to_rel(file_path, workspace)
                if rel not in changed_set:
                    continue
                violations.append(Violation(
                    file=rel,
                    line=finding.get("StartLine"),
                    severity="high",
                    message=f"Secret detectado: {finding.get('RuleID', 'unknown')}",
                ))

            status = GateStatus.failed if violations else GateStatus.passed
            return CheckResult(
                check=self.name,
                status=status,
                metrics={"secrets_found": len(violations)},
                violations=violations,
            )
        finally:
            if owns_report_dir:
                shutil.rmtree(report_dir, ignore_errors=True)

    @staticmethod
    def _to_rel(path: str, workspace: Path) -> str:
        try:
            return str(Path(path).relative_to(workspace))
        except ValueError:
            return path
