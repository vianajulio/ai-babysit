"""Camada de revisão semântica: contexto para o subagente e retorno dos findings.

O julgamento acontece fora do servidor. Aqui só entram o empacotamento do
contexto (diff, padrão de código, schema de finding) e a conversão do que o
subagente devolve em um `CheckResult` que a agregação já sabe consolidar.
"""
import json
from pathlib import Path
from typing import Annotated

from app.gates import git_diff, review_plan
from app.mcp.plan_state import (
    CONTEXT_INSTRUCTIONS,
    deterministic_results_for,
    load_plan_task,
)
from app.mcp.runtime import ensure_db, exc_err, mcp
from app.storage import repositories


@mcp.tool()
async def get_task_context(
    plan_id: Annotated[str, "ID do plano retornado por plan_pr_review"],
    task_id: Annotated[str, "ID de uma task de revisão semântica (ex.: 'review-1')"],
) -> str:
    """Entrega a um subagente o contexto de uma task de revisão semântica: diff por arquivo,
    caminho do worktree para leitura do entorno, o padrão de código do projeto, o schema de
    finding e os resultados determinísticos já calculados para esses arquivos. O julgamento
    acontece fora do servidor; o retorno volta por submit_task_findings. Quando o diff não cabe
    no orçamento de contexto, os arquivos cortados aparecem em `truncated` — nunca há
    truncamento silencioso."""
    try:
        ensure_db()
        record, plan, runtime, task = load_plan_task(plan_id, task_id, kind="agent")

        repo_root = Path(runtime.get("repo_root") or record["workspace"])
        worktree_path = runtime.get("worktree") or str(repo_root)
        paths = list(task["files"])
        diffs = git_diff.file_diffs(
            repo_root, runtime.get("base", ""), runtime.get("head", ""), paths
        )

        pr_cfg = runtime.get("config", {}).get("quality_gate", {}).get("pr_review", {})
        budget = pr_cfg.get("max_context_chars", 120000)
        weights = runtime.get("weights", {})

        files: list[dict] = []
        truncated: list[str] = []
        used = 0
        for path in paths:
            diff = diffs.get(path, "")
            if used + len(diff) > budget:
                diff = diff[: max(budget - used, 0)]
                truncated.append(path)
            used += len(diff)
            files.append({
                "path": path,
                "added_lines": weights.get(path, 0),
                "diff": diff,
            })

        from app.ai.prompts import FINDING_SCHEMA, load_standards

        return json.dumps({
            "plan_id": plan_id,
            "task_id": task_id,
            "worktree_path": worktree_path,
            "volatile": bool(runtime.get("volatile", False)),
            "files": files,
            "truncated": truncated,
            "deterministic_checks": deterministic_results_for(plan_id, plan, paths),
            "standards": load_standards(repo_root),
            "finding_schema": dict(FINDING_SCHEMA),
            "instructions": CONTEXT_INSTRUCTIONS,
        })
    except Exception as exc:
        return exc_err(exc)


@mcp.tool()
async def submit_task_findings(
    plan_id: Annotated[str, "ID do plano retornado por plan_pr_review"],
    task_id: Annotated[str, "ID da task de revisão semântica revisada (ex.: 'review-1')"],
    findings: Annotated[
        list[dict],
        "Lista de findings no formato de finding_schema: file, line, severity (high|medium|low), "
        "category, message, suggestion. Lista vazia significa 'revisado e limpo'.",
    ],
) -> str:
    """Recebe o resultado da revisão semântica de um subagente e persiste como resultado parcial
    da task (check `agent_review`), no mesmo lugar em que run_review_task grava — reenviar a mesma
    task sobrescreve, não duplica. `high` reprova o gate quando agent_review.block_on é `high`;
    com `none`, o pior status possível é warning. Devolve um resumo enxuto; a tabela consolidada
    só sai de get_review_plan."""
    try:
        ensure_db()
        _record, _plan, runtime, _task = load_plan_task(plan_id, task_id, kind="agent")

        agent_cfg = (
            runtime.get("config", {})
            .get("quality_gate", {})
            .get("checks", {})
            .get("agent_review", {})
        )
        check = review_plan.findings_to_check_result(
            list(findings or []), agent_cfg.get("block_on", "high")
        )

        repositories.save_review_task_result(
            plan_id=plan_id,
            task_id=task_id,
            status="done",
            result=[check.model_dump(mode="json")],
        )

        all_ids = {task["task_id"] for task in _plan.get("tasks", [])}
        done_ids = {
            stored["task_id"]
            for stored in repositories.load_review_tasks(plan_id)
            if stored["status"] == "done"
        }
        return json.dumps({
            "plan_id": plan_id,
            "task_id": task_id,
            "accepted": len(check.violations),
            "by_severity": {
                "high": check.metrics["high_issues"],
                "medium": check.metrics["medium_issues"],
                "low": check.metrics["low_issues"],
            },
            "remaining_tasks": len(all_ids - done_ids),
        })
    except Exception as exc:
        return exc_err(exc)
