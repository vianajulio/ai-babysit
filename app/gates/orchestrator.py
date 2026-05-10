import uuid
from pathlib import Path

from app.gates import ratchet, result
from app.gates.models import CheckResult, GateRun, GateStatus
from app.providers import azure_devops
from app.runners.ai_review import AIReviewRunner
from app.runners.complexity import ComplexityRunner
from app.runners.duplication import DuplicationRunner
from app.runners.file_size import FileSizeRunner
from app.runners.secrets import SecretsRunner
from app.storage import repositories
from app.workspaces import manager
from settings import settings


def _build_runners(config: dict) -> list:
    checks = config.get("quality_gate", {}).get("checks", {})
    runners = []

    if checks.get("file_size", {}).get("enabled", True):
        cfg = checks.get("file_size", {})
        runners.append(FileSizeRunner(
            max_lines_per_file=cfg.get("max_lines_per_file", 400),
            max_lines_per_function=cfg.get("max_lines_per_function", 80),
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

    return runners


async def _run_checks(workspace: Path, changed_files: list[str]) -> list[CheckResult]:
    config = settings.load_quality_gate_config(workspace)
    runners = _build_runners(config)
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


async def _annotate_with_ai(checks: list[CheckResult]) -> list[CheckResult]:
    ai_cfg = settings.quality_gate_config.get("quality_gate", {}).get("checks", {}).get("ai_review", {})
    if not ai_cfg.get("enabled", True):
        return checks

    ai_runner = AIReviewRunner()
    return await ai_runner.annotate(checks)


async def run_local_quality_gate(
    workspace: Path,
    changed_files: list[str],
    repository: str = "local",
    branch: str = "local",
    use_ratchet: bool = False,
) -> dict:
    run_id = str(uuid.uuid4())
    checks = await _run_checks(workspace, changed_files)

    baseline = repositories.load_baseline(repository, branch) if use_ratchet else None
    checks = ratchet.apply(checks, baseline)
    checks = await _annotate_with_ai(checks)

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


async def run_quality_gate(pr_id: int) -> dict:
    run_id = str(uuid.uuid4())

    pr = await azure_devops.get_pull_request(pr_id)
    source_branch = pr["sourceRefName"].replace("refs/heads/", "")
    target_branch = pr["targetRefName"].replace("refs/heads/", "")
    repository = settings.azure_repo

    clone_url = azure_devops.get_clone_url()
    changed_files = await azure_devops.get_changed_files(pr_id)

    workspace: Path | None = None
    checks: list[CheckResult] = []

    try:
        workspace = await manager.prepare_workspace(run_id, clone_url, source_branch)

        checks = await _run_checks(workspace, changed_files)

    finally:
        if workspace and settings.babysit_cleanup_after_run:
            manager.cleanup_workspace(workspace)

    baseline = repositories.load_baseline(repository, target_branch)
    checks = ratchet.apply(checks, baseline)

    checks = await _annotate_with_ai(checks)

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
