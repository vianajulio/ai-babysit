import json
import re
from pathlib import Path

import httpx
from fastapi import HTTPException

from app.ai import ollama
from app.ai.prompts import build_big_o_prompt
from app.gates.models import CheckResult, GateStatus, Violation

_LOOP_RE = re.compile(r"\b(for|foreach|while)\b")
_LINQ_RE = re.compile(r"\.(Where|Select|GroupBy|OrderBy|ThenBy|Any|First|Single|Count)\s*\(")
_REPOSITORY_RE = re.compile(r"\b(repository|repo|dbContext|context|DbSet)\b|\.Get\w*Async\s*\(", re.IGNORECASE)
_MATERIALIZE_RE = re.compile(r"\.(ToList|ToArray|OrderBy|ThenBy)\s*\(")
_MEMBERSHIP_RE = re.compile(r"\.(Contains|Any|First|Single)\s*\(")
_GENERATED_SUFFIXES = (".g.cs", ".generated.cs", ".designer.cs")


class BigORunner:
    name = "big_o"

    def __init__(
        self,
        languages: list[str] | None = None,
        fail_on_high: bool = True,
        warn_on_medium: bool = True,
        max_files_per_run: int = 20,
        max_code_chars: int = 1000,
    ):
        self.languages = languages or ["csharp"]
        self.fail_on_high = fail_on_high
        self.warn_on_medium = warn_on_medium
        self.max_files_per_run = max_files_per_run
        self.max_code_chars = max_code_chars

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        files = self._eligible_files(workspace, changed_files)
        metrics = self._empty_metrics(files_considered=len(files))
        if not files:
            return CheckResult(check=self.name, status=GateStatus.skipped, metrics=metrics)

        candidates = [(rel, code) for rel, code in files if self._is_candidate(code)]
        candidates = candidates[:self.max_files_per_run]
        metrics["files_analyzed"] = len(candidates)
        if not candidates:
            return CheckResult(check=self.name, status=GateStatus.skipped, metrics=metrics)

        violations: list[Violation] = []
        low_issues = 0

        for rel_path, code in candidates:
            try:
                payload = await self._analyze_file(rel_path, code)
            except httpx.TimeoutException:
                return CheckResult(
                    check=self.name,
                    status=GateStatus.skipped,
                    metrics={**metrics, "error": "Ollama timeout ao analisar Big O"},
                )
            except HTTPException as exc:
                return CheckResult(
                    check=self.name,
                    status=GateStatus.skipped,
                    metrics={**metrics, "error": str(exc.detail)},
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                return CheckResult(
                    check=self.name,
                    status=GateStatus.error,
                    metrics={**metrics, "error": f"Big O JSON invalido: {str(exc)[:120]}"},
                )

            for issue in payload.get("issues", []):
                severity = str(issue.get("severity", "low")).lower()
                if severity == "low":
                    low_issues += 1
                    continue
                violations.append(self._to_violation(rel_path, issue, severity))

        high_issues = sum(1 for v in violations if v.severity == "high")
        medium_issues = sum(1 for v in violations if v.severity == "medium")
        metrics.update({
            "high_issues": high_issues,
            "medium_issues": medium_issues,
            "low_issues": low_issues,
            "violations_count": len(violations),
        })

        return CheckResult(
            check=self.name,
            status=self._status(high_issues, medium_issues),
            metrics=metrics,
            violations=violations,
        )

    def _eligible_files(self, workspace: Path, changed_files: list[str]) -> list[tuple[str, str]]:
        if "csharp" not in {lang.lower() for lang in self.languages}:
            return []

        files = []
        for rel_path in changed_files:
            clean_path = rel_path.lstrip("/")
            lower_path = clean_path.lower()
            if not lower_path.endswith(".cs") or lower_path.endswith(_GENERATED_SUFFIXES):
                continue

            full_path = workspace / clean_path
            if not full_path.exists():
                continue

            try:
                files.append((clean_path, full_path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        return files

    def _is_candidate(self, code: str) -> bool:
        if len(_LINQ_RE.findall(code)) >= 2:
            return True
        if not _LOOP_RE.search(code):
            return False
        return any(pattern.search(code) for pattern in (
            _LINQ_RE,
            _REPOSITORY_RE,
            _MATERIALIZE_RE,
            _MEMBERSHIP_RE,
        ))

    async def _analyze_file(self, rel_path: str, code: str) -> dict:
        prompt = build_big_o_prompt(language="csharp", file_path=rel_path, code=code[:self.max_code_chars])
        raw = await ollama.generate(prompt, expect_json=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("issues", []), list):
            raise ValueError("resposta sem lista issues")
        return payload

    def _to_violation(self, rel_path: str, issue: dict, severity: str) -> Violation:
        current = issue.get("current_complexity", "")
        suggested = issue.get("suggested_complexity", "")
        problem = issue.get("problem", "Risco de complexidade algoritmica")
        suggestion = issue.get("suggestion", "Revise a estrutura de dados ou consulta usada")
        message = f"Possivel {current}: {problem}. Sugestao: {suggestion}"
        if suggested:
            message = f"{message} Complexidade sugerida: {suggested}"

        return Violation(
            file=rel_path,
            line=issue.get("line"),
            severity=severity,
            message=message,
        )

    def _status(self, high_issues: int, medium_issues: int) -> GateStatus:
        if high_issues and self.fail_on_high:
            return GateStatus.failed
        if high_issues or (medium_issues and self.warn_on_medium):
            return GateStatus.warning
        return GateStatus.passed

    @staticmethod
    def _empty_metrics(files_considered: int) -> dict:
        return {
            "files_considered": files_considered,
            "files_analyzed": 0,
            "high_issues": 0,
            "medium_issues": 0,
            "low_issues": 0,
            "violations_count": 0,
        }
