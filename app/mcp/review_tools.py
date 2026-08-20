"""Tools do plano de revisão: partição, fan-out determinístico e camada de subagente."""
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from app.gates import git_diff, review_plan
from app.gates.models import CheckResult
from app.gates.orchestrator import run_review_task_gate
from app.mcp.plan_state import (
    cleanup_plan_runtime,
    delete_review_plan_records,
    expire_stale_plans,
    load_plan_task,
)
from app.mcp.runtime import ensure_db, exc_err, mcp
from app.mcp.workspace import auto_branch, auto_repository, cleanup_worktree, git
from app.storage import repositories
from settings import settings


@mcp.tool()
async def plan_pr_review(
    workspace: Annotated[str, "Raiz do repositório Git a revisar"],
    base_ref: Annotated[str, "Referência base do PR (ex.: origin/main)"],
    head_ref: Annotated[str, "Referência de topo; padrão HEAD"] = "HEAD",
    repository: Annotated[str, "Identificador opcional para baseline"] = "",
    branch: Annotated[str, "Branch opcional para baseline"] = "",
    mode: Annotated[str, "auto (padrão, decide por tamanho) | inline (força one_shot) | sharded (força fan-out)"] = "auto",
) -> str:
    """Planeja a revisão de um PR. Se o diff for pequeno, devolve mode=one_shot com uma única
    chamada a run_local_gate. Se for grande, devolve mode=sharded com tasks paralelizáveis:
    execute cada task com run_review_task (em agentes distintos, respeitando parallel_hint) e
    depois chame get_review_plan para o resultado consolidado."""
    repo_root: Path | None = None
    worktree: Path | None = None
    temp_root: Path | None = None
    added = False
    try:
        ws = Path(workspace).resolve()
        if not ws.is_dir():
            raise ValueError(f"workspace não é um diretório: {workspace}")

        try:
            repo_root = Path(git(["rev-parse", "--show-toplevel"], ws).strip()).resolve()
        except RuntimeError as exc:
            raise ValueError(f"workspace não é um repositório git: {workspace}") from exc

        ensure_db()
        config = settings.load_quality_gate_config(repo_root)
        pr_review_cfg = config.get("quality_gate", {}).get("pr_review", {})
        expire_stale_plans(pr_review_cfg.get("plan_ttl_minutes", 60))

        # `head_ref=WORKTREE` revisa a árvore de trabalho viva: não há commit a
        # resolver nem worktree a criar, e o resultado é volátil por natureza
        # (o usuário pode editar durante o fan-out).
        volatile = head_ref.strip().upper() == git_diff.WORKTREE_REF
        resolved_base = git_diff.resolve_commit(repo_root, base_ref)
        resolved_head = (
            git_diff.WORKTREE_REF if volatile else git_diff.resolve_commit(repo_root, head_ref)
        )
        changed_files = git_diff.changed_files_with_weight(repo_root, resolved_base, resolved_head)
        if not changed_files:
            raise ValueError("nenhum arquivo alterado entre as referências")

        plan_id = str(uuid.uuid4())
        # Import tardio: `app.ai.prompts` só lê arquivo e config, mas manter o
        # padrão do módulo de não puxar `app.ai` no topo do servidor.
        from app.ai.prompts import load_standards

        plan = review_plan.build_plan(
            files=changed_files,
            config=config,
            plan_id=plan_id,
            workspace=str(repo_root),
            standards=load_standards(repo_root),
            mode=mode,
        )

        selected_repository = auto_repository(str(repo_root), repository)
        selected_branch = branch or auto_branch(str(repo_root), "")

        if plan["mode"] == "sharded" and not volatile:
            # Uma revisão sobre commit/branch que não é o checkout atual usa um único
            # worktree detached (como run_commit_gate já faz); todas as tasks o reusam
            # para não multiplicar custo criando um worktree por task.
            temp_root = Path(tempfile.mkdtemp(prefix="babysit-review-plan-"))
            worktree = temp_root / "repo"
            git(["worktree", "add", "--detach", str(worktree), resolved_head], repo_root)
            added = True

        plan["volatile"] = volatile
        payload = {
            "plan": plan,
            "runtime": {
                "repo_root": str(repo_root),
                "worktree": str(worktree) if worktree else None,
                "temp_root": str(temp_root) if temp_root else None,
                "config": config,
                "repository": selected_repository,
                "branch": selected_branch,
                # Revisão de árvore viva nunca vira baseline: o diff pode mudar
                # entre uma task e outra.
                "use_ratchet": False,
                "volatile": volatile,
                # Guardados para `get_task_context` recalcular o diff de uma
                # fatia sem repetir a resolução de refs.
                "base": resolved_base,
                "head": resolved_head,
                "weights": {file.path: file.added_lines for file in changed_files},
            },
        }
        repositories.save_review_plan(
            plan_id=plan_id,
            workspace=str(repo_root),
            repository=selected_repository,
            branch=selected_branch,
            status="pending",
            payload=payload,
        )
        return json.dumps(plan)
    except Exception as exc:
        if repo_root is not None:
            cleanup_worktree(repo_root, worktree if added else None, temp_root)
        elif temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
        return exc_err(exc)


@mcp.tool()
async def run_review_task(
    plan_id: Annotated[str, "ID do plano retornado por plan_pr_review"],
    task_id: Annotated[str, "ID da task a executar (ex.: 'global', 'files-1')"],
    detail: Annotated[
        str,
        "summary (padrão, retorno enxuto) ou full (inclui os CheckResult completos da task, "
        "para um subagente que precisa do achado determinístico como insumo)",
    ] = "summary",
) -> str:
    """Executa somente os checks de uma task do plano de revisão (sobre um subconjunto dos
    arquivos alterados) e persiste o resultado parcial por (plan_id, task_id) — repetir a
    chamada para a mesma task é seguro e apenas sobrescreve o resultado anterior. Devolve um
    resumo enxuto; a tabela consolidada só sai de get_review_plan, depois que todas as tasks
    do plano estiverem concluídas."""
    try:
        ensure_db()
        if detail not in ("summary", "full"):
            raise ValueError(f"detail inválido: {detail!r}; use 'summary' ou 'full'")

        record, plan, runtime, task = load_plan_task(plan_id, task_id, kind="deterministic")
        tasks_by_id = {item["task_id"]: item for item in plan.get("tasks", [])}

        workspace_path = runtime.get("worktree") or runtime.get("repo_root") or record["workspace"]
        results = await run_review_task_gate(
            workspace=Path(workspace_path),
            changed_files=task["files"],
            checks=task["checks"],
        )

        repositories.save_review_task_result(
            plan_id=plan_id,
            task_id=task_id,
            status="done",
            result=[check.model_dump(mode="json") for check in results],
        )

        all_ids = set(tasks_by_id.keys())
        done_ids = {t["task_id"] for t in repositories.load_review_tasks(plan_id) if t["status"] == "done"}
        remaining = len(all_ids - done_ids)

        severity = {"passed": 0, "skipped": 0, "warning": 1, "failed": 2, "error": 3}
        worst = max(
            (check.status.value for check in results),
            key=lambda status: severity.get(status, 0),
            default="passed",
        )
        violations = sum(len(check.violations) for check in results)

        summary = {
            "plan_id": plan_id,
            "task_id": task_id,
            "status": worst,
            "violations": violations,
            "remaining_tasks": remaining,
        }
        if detail == "full":
            # Só quando pedido: a tabela consolidada continua sendo o formato
            # final, e devolver as violations por padrão encheria o contexto do
            # agente que apenas dispara a task.
            summary["checks"] = [check.model_dump(mode="json") for check in results]
        return json.dumps(summary)
    except Exception as exc:
        return exc_err(exc)




@mcp.tool()
async def get_review_plan(
    plan_id: Annotated[str, "ID do plano retornado por plan_pr_review"],
    force: Annotated[
        bool,
        "Se true, consolida mesmo com tasks pendentes: as faltantes viram 'skipped' e o "
        "veredito nunca sai como passed limpo. Use quando um subagente morreu ou o cliente "
        "desistiu da revisão semântica.",
    ] = False,
) -> str:
    """Devolve {"status": "pending", "pending_tasks": [...]} enquanto faltar task, ou a
    tabela markdown consolidada quando todas as tasks estiverem concluídas — mesmo formato
    de saída de run_local_gate/run_commit_gate. Ratchet e baseline só são aplicados aqui,
    uma única vez sobre o resultado agregado, e nada vindo de agent_review vira baseline.
    Chamadas repetidas depois de completo devolvem a mesma tabela, sem reprocessar."""
    try:
        ensure_db()
        record = repositories.load_review_plan(plan_id)
        if record is None:
            raise ValueError(f"plano '{plan_id}' não encontrado")

        payload = record["payload"]
        if record["status"] == "complete" and "table" in payload:
            return payload["table"]

        plan = payload.get("plan", {})
        all_ids = [task["task_id"] for task in plan.get("tasks", [])]
        task_results = repositories.load_review_tasks(plan_id)
        done = {t["task_id"]: t for t in task_results if t["status"] == "done"}
        pending = [task_id for task_id in all_ids if task_id not in done]

        if pending and not force:
            return json.dumps({"status": "pending", "pending_tasks": pending})

        parts = [
            [CheckResult(**check) for check in (done[task_id]["result"] or [])]
            for task_id in all_ids
            if task_id in done
        ]
        runtime = payload.get("runtime", {})
        table = await review_plan.finalize_plan(
            plan,
            parts,
            config=runtime.get("config", {}),
            repository=runtime.get("repository", record["repository"]),
            branch=runtime.get("branch", record["branch"]),
            use_ratchet=runtime.get("use_ratchet", False),
            run_id=plan_id,
            forced_tasks=pending,
        )

        completed_payload = {**payload, "table": table}
        repositories.save_review_plan(
            plan_id=plan_id,
            workspace=record["workspace"],
            repository=record["repository"],
            branch=record["branch"],
            status="complete",
            payload=completed_payload,
        )
        return table
    except Exception as exc:
        return exc_err(exc)


@mcp.tool()
async def close_review_plan(
    plan_id: Annotated[str, "ID do plano a encerrar"],
) -> str:
    """Remove o worktree e os registros de um plano de revisão. Chame sempre ao final do
    fluxo — mesmo que o cliente não conclua todas as tasks — para não deixar worktree órfão."""
    try:
        ensure_db()
        record = repositories.load_review_plan(plan_id)
        if record is None:
            raise ValueError(f"plano '{plan_id}' não encontrado")

        cleanup_plan_runtime(record["payload"].get("runtime", {}))
        delete_review_plan_records(plan_id)
        return json.dumps({"plan_id": plan_id, "closed": True})
    except Exception as exc:
        return exc_err(exc)

