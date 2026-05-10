import asyncio
import re
from pathlib import Path

from app.gates.models import CheckResult, GateStatus, Violation
from app.runners.tools import resolve_tool
from settings import settings

_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+\S+\s+(\S+)\s+\d+\s+\d+\s+(.+)$"
)
_AVG_RE = re.compile(r"Average cyclomatic complexity:\s+([\d.]+)")


class ComplexityRunner:
    name = "complexity"

    def __init__(self, max_cyclomatic: int = 10, cannot_increase_average: bool = True):
        self.max_cyclomatic = max_cyclomatic
        self.cannot_increase_average = cannot_increase_average

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        cmd = [resolve_tool("lizard"), str(workspace), "--csv", "-C", str(self.max_cyclomatic)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return CheckResult(check=self.name, status=GateStatus.error,
                               metrics={"error": "lizard timeout"})
        except FileNotFoundError:
            return CheckResult(check=self.name, status=GateStatus.skipped,
                               metrics={"error": "lizard não instalado"})

        output = stdout.decode(errors="replace")
        violations = self._parse_violations(output, changed_files)
        avg = self._parse_average(stderr.decode(errors="replace") + output)

        metrics: dict = {"violations_count": len(violations)}
        if avg is not None:
            metrics["average_complexity"] = avg

        status = GateStatus.failed if violations else GateStatus.passed
        return CheckResult(check=self.name, status=status, metrics=metrics, violations=violations)

    def _parse_violations(self, output: str, changed_files: list[str]) -> list[Violation]:
        violations = []
        changed_set = {f.lstrip("/") for f in changed_files}

        for line in output.splitlines():
            # CSV format: complexity,lines,tokens,file,line,function
            parts = line.split(",")
            if len(parts) < 6:
                continue
            try:
                complexity = int(parts[0].strip())
                file_path = parts[3].strip()
                line_no = int(parts[4].strip())
                func_name = parts[5].strip()
            except (ValueError, IndexError):
                continue

            rel = self._to_rel(file_path)
            if rel not in changed_set:
                continue

            if complexity > self.max_cyclomatic:
                violations.append(Violation(
                    file=rel,
                    line=line_no,
                    severity="high" if complexity > self.max_cyclomatic * 1.5 else "medium",
                    message=f"Função `{func_name}` com complexidade ciclomática {complexity}",
                    current_value=complexity,
                    allowed_value=self.max_cyclomatic,
                ))

        return violations

    def _parse_average(self, text: str) -> float | None:
        m = _AVG_RE.search(text)
        return float(m.group(1)) if m else None

    @staticmethod
    def _to_rel(path: str) -> str:
        parts = path.split("/")
        return "/".join(parts[parts.index("repo") + 1:]) if "repo" in parts else path
