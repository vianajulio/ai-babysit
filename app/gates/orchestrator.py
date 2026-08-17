import inspect
import uuid
from pathlib import Path

from app.gates import ratchet, result
from app.gates.models import CheckResult, GateRun, GateStatus
from app.runners.complexity import ComplexityRunner
from app.runners.duplication import DuplicationRunner
from app.runners.file_size import FileSizeRunner
from app.runners.secrets import SecretsRunner
from app.storage import repositories
from app.workspaces import manager
from settings import settings


def _build_runners(config: dict, only: list[str] | None = None) -> list:
    checks = config.get("quality_gate", {}).get("checks", {})
    runners = []

    if checks.get("file_size", {}).get("enabled", True):
        cfg = checks.get("file_size", {})
        runners.append(FileSizeRunner(
            max_lines_per_file=cfg.get("max_lines_per_file", 400),
            max_lines_per_function=cfg.get("max_lines_per_function", 80),
            exclude=cfg.get("exclude", []),
        ))

    if checks.get("complexity", {}).get("enabled", True):
        cfg = checks.get("complexity", {})
        runners.append(ComplexityRunner(
            max_cyclomatic=cfg.get("max_cyclomatic_complexity", 10),
            cannot_increase_average=cfg.get("cannot_increase_average", True),
        ))

    if checks.get("duplication", {}).get("enabled", True):
        cfg = checks.get("duplication", {})
        runners.append(DuplicationRunner(
            max_percent=cfg.get("max_percent", 5.0),
            fail_only_on_changed_files=cfg.get("fail_only_on_changed_files", False),
        ))

    if checks.get("secrets", {}).get("enabled", True):
        runners.append(SecretsRunner())

    if checks.get("big_o", {}).get("enabled", False):
        # Big O is the only regular runner that talks to Ollama.  Keep it
        # lazy so a local gate does not require the optional AI dependency.
        from app.runners.big_o import BigORunner

        cfg = checks.get("big_o", {})
        runners.append(BigORunner(
            languages=cfg.get("languages", ["csharp"]),
            fail_on_high=cfg.get("fail_on_high", True),
            warn_on_medium=cfg.get("warn_on_medium", True),
            max_files_per_run=cfg.get("max_files_per_run", 20),
            max_code_chars=cfg.get("max_code_chars", 1000),
        ))

    if only is not None:
        runners = [runner for runner in runners if runner.name in only]

    return runners


async def _run_checks(
    workspace: Path,
    changed_files: list[str],
    only: list[str] | None = None,
) -> list[CheckResult]:
    config = settings.load_quality_gate_config(workspace)
    runners = _build_runners(config, only=only)
    checks: list[CheckResult] = []

    for runner in runners:
        try:
            check_result = await runner.run(workspace, changed_files)
        except Exception as exc:
            check_result = CheckResult(
                check=runner.name,
                status=GateStatus.error,
                metrics={"error": str(exc)[:200]},
            )
        checks.append(check_result)

    return checks


class AIReviewRunner:
    """Lazy AI annotator kept out of the local gate's import path."""

    async def annotate(self, checks: list[CheckResult]) -> list[CheckResult]:
        from app.ai import ollama
        from app.ai.prompts import build_suggestion_prompt

        annotated: list[CheckResult] = []
        unavailable: str | None = None
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
                suggestion = (await ollama.generate(prompt)).strip()
            except Exception as exc:
                detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
                unavailable = f"Ollama indisponível para revisão IA: {detail}"
                suggestion = ""

            annotated.append(check.model_copy(update={"suggestion": suggestion}))

        if unavailable:
            # Keep the original gate result intact while making an optional
            # service failure visible to the caller.
            annotated.append(CheckResult(
                check="ai_review",
                status=GateStatus.warning,
                metrics={"error": unavailable},
            ))

        return annotated


async def _annotate_with_ai(
    checks: list[CheckResult],
    config: dict | None = None,
) -> list[CheckResult]:
    """Add optional AI suggestions using this workspace's merged config."""
    ai_cfg = (config or {}).get("quality_gate", {}).get("checks", {}).get("ai_review", {})
    if not ai_cfg.get("enabled", False):
        return checks
    return await AIReviewRunner().annotate(checks)


async def _apply_ai_annotation(checks: list[CheckResult], config: dict) -> list[CheckResult]:
    """Call the annotator while retaining compatibility with small test hooks."""
    parameters = inspect.signature(_annotate_with_ai).parameters
    if "config" in parameters or len(parameters) > 1:
        return await _annotate_with_ai(checks, config)
    return await _annotate_with_ai(checks)


async def run_local_quality_gate(
    workspace: Path,
    changed_files: list[str],
    repository: str = "local",
    branch: str = "local",
    use_ratchet: bool = False,
) -> dict:
    run_id = str(uuid.uuid4())
    config = settings.load_quality_gate_config(workspace)
    checks = await _run_checks(workspace, changed_files)

    baseline = repositories.load_baseline(repository, branch) if use_ratchet else None
    checks = ratchet.apply(checks, baseline)
    checks = await _apply_ai_annotation(checks, config)

    gate_run = result.consolidate(
        run_id=run_id,
        pr_id=0,
        checks=checks,
        repository=repository,
        source_branch=branch,
        target_branch=branch,
    )

    current_metrics = ratchet.extract_metrics(checks)
    if use_ratchet and gate_run.status == GateStatus.passed:
        merged = {**baseline, **current_metrics} if baseline else current_metrics
        repositories.save_baseline(repository, branch, merged)

    repositories.save_gate_run(gate_run)
    return gate_run.model_dump(mode="json")


async def run_review_task_gate(
    workspace: Path,
    changed_files: list[str],
    checks: list[str],
) -> list[CheckResult]:
    """Run only the checks requested for one review-plan task.

    Returns the raw ``CheckResult`` list: no ``ratchet.apply``, no
    ``save_baseline``, no AI annotation, no ``result.consolidate``. Those
    steps are exclusively the aggregation stage's responsibility (Task 6) —
    applying ratchet/baseline to a partial slice of the PR would record a
    baseline (or a pass/fail verdict) derived from an incomplete diff.
    """
    return await _run_checks(workspace, changed_files, only=checks)


async def run_quality_gate(pr_id: int) -> dict:
    run_id = str(uuid.uuid4())

    # Keep the Azure integration available to the HTTP/remote path without
    # importing it when the standalone local MCP server is loaded.
    from app.providers import azure_devops

    pr = await azure_devops.get_pull_request(pr_id)
    source_branch = pr["sourceRefName"].replace("refs/heads/", "")
    target_branch = pr["targetRefName"].replace("refs/heads/", "")
    repository = settings.azure_repo

    clone_url = azure_devops.get_clone_url()
    changed_files = await azure_devops.get_changed_files(pr_id)

    workspace: Path | None = None
    checks: list[CheckResult] = []
    config: dict = {}

    try:
        workspace = await manager.prepare_workspace(run_id, clone_url, source_branch)

        checks = await _run_checks(workspace, changed_files)
        config = settings.load_quality_gate_config(workspace)

    finally:
        if workspace and settings.babysit_cleanup_after_run:
            manager.cleanup_workspace(workspace)

    baseline = repositories.load_baseline(repository, target_branch)
    checks = ratchet.apply(checks, baseline)

    checks = await _apply_ai_annotation(checks, config)

    gate_run = result.consolidate(
        run_id=run_id,
        pr_id=pr_id,
        checks=checks,
        repository=repository,
        source_branch=source_branch,
        target_branch=target_branch,
    )

    current_metrics = ratchet.extract_metrics(checks)
    if gate_run.status == GateStatus.passed:
        if baseline:
            merged = {**baseline, **current_metrics}
        else:
            merged = current_metrics
        repositories.save_baseline(repository, target_branch, merged)

    repositories.save_gate_run(gate_run)

    comment = result.build_pr_comment(gate_run)
    try:
        await azure_devops.post_comment(pr_id, comment)
        gate_run = gate_run.model_copy(update={"comment_posted": True})
        repositories.save_gate_run(gate_run)
    except Exception:
        pass

    return gate_run.model_dump(mode="json")
