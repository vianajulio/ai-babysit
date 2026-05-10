from pathlib import Path

from app.ai import ollama
from app.ai.prompts import build_suggestion_prompt
from app.gates.models import CheckResult, GateStatus


class AIReviewRunner:
    name = "ai_review"

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        # This runner operates on results from other runners, not the workspace directly.
        # It is called explicitly by the orchestrator after other runners finish.
        return CheckResult(check=self.name, status=GateStatus.skipped)

    async def annotate(self, checks: list[CheckResult]) -> list[CheckResult]:
        annotated = []
        for check in checks:
            if check.status != GateStatus.failed or not check.violations:
                annotated.append(check)
                continue

            violations_payload = [
                {"file": v.file, "line": v.line, "message": v.message}
                for v in check.violations[:5]
            ]
            prompt = build_suggestion_prompt(check.check, violations_payload)
            try:
                suggestion = await ollama.generate(prompt)
                suggestion = suggestion.strip()
            except Exception:
                suggestion = ""

            annotated.append(check.model_copy(update={"suggestion": suggestion}))

        return annotated
