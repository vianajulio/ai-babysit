"""Estado persistido de um plano de revisão: limpeza, expiração e leitura de task."""
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from app.gates import review_plan
from app.gates.models import CheckResult
from app.mcp.workspace import cleanup_worktree
from app.storage import database, repositories
from app.storage.database import ReviewPlanRecord, ReviewTaskRecord


def cleanup_plan_runtime(runtime: dict) -> None:
    """Libera o worktree/diretório temporário guardados em `payload.runtime`."""
    repo_root = runtime.get("repo_root")
    worktree = runtime.get("worktree")
    temp_root = runtime.get("temp_root")
    if repo_root and worktree:
        cleanup_worktree(Path(repo_root), Path(worktree), Path(temp_root) if temp_root else None)
    elif temp_root:
        shutil.rmtree(temp_root, ignore_errors=True)


def delete_review_plan_records(plan_id: str) -> None:
    with database.get_session() as session:
        session.execute(delete(ReviewTaskRecord).where(ReviewTaskRecord.plan_id == plan_id))
        session.execute(delete(ReviewPlanRecord).where(ReviewPlanRecord.plan_id == plan_id))
        session.commit()


def expire_stale_plans(ttl_minutes: int) -> None:
    """Limpa planos inativos há mais de `plan_ttl_minutes`, liberando worktrees
    órfãos deixados por clientes que nunca chamaram `close_review_plan`.

    O corte é por **atividade**, não por criação: um fan-out longo mantém o
    plano vivo enquanto tasks retornam, e um plano já consolidado (`complete`)
    que ninguém fechou também expira — era o caso mais comum de worktree
    vazado, porque o cliente costuma parar na tabela.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=max(ttl_minutes, 0))
    with database.get_session() as session:
        last_activity = {
            plan_id: updated_at
            for plan_id, updated_at in session.execute(
                select(ReviewTaskRecord.plan_id, func.max(ReviewTaskRecord.updated_at))
                .group_by(ReviewTaskRecord.plan_id)
            )
        }
        candidates = session.scalars(
            select(ReviewPlanRecord).where(ReviewPlanRecord.created_at < cutoff)
        ).all()
        stale = [
            record
            for record in candidates
            if last_activity.get(record.plan_id, record.created_at) < cutoff
        ]
        for record in stale:
            try:
                payload = json.loads(record.payload_json)
                cleanup_plan_runtime(payload.get("runtime", {}))
            except Exception:
                pass
            session.execute(delete(ReviewTaskRecord).where(ReviewTaskRecord.plan_id == record.plan_id))
            session.delete(record)
        session.commit()



def load_plan_task(plan_id: str, task_id: str, *, kind: str) -> tuple[dict, dict, dict, dict]:
    """Carrega plano, runtime e task, validando o tipo esperado da task.

    `kind` separa as duas famílias de task: `deterministic` roda no servidor
    (`run_review_task`), `agent` é revisada fora dele (`get_task_context` +
    `submit_task_findings`). Chamar a tool errada devolve um erro que aponta a
    tool certa em vez de um resultado silenciosamente vazio.
    """
    record = repositories.load_review_plan(plan_id)
    if record is None:
        raise ValueError(f"plano '{plan_id}' não encontrado")

    plan = record["payload"].get("plan", {})
    runtime = record["payload"].get("runtime", {})
    tasks_by_id = {task["task_id"]: task for task in plan.get("tasks", [])}
    task = tasks_by_id.get(task_id)
    if task is None:
        raise ValueError(f"task '{task_id}' não encontrada no plano '{plan_id}'")

    actual = task.get("kind", "deterministic")
    if actual != kind:
        expected_tool = (
            "run_review_task"
            if actual == "deterministic"
            else "get_task_context / submit_task_findings"
        )
        raise ValueError(
            f"task '{task_id}' é do tipo '{actual}': use {expected_tool} para ela"
        )
    return record, plan, runtime, task


def deterministic_results_for(plan_id: str, plan: dict, paths: list[str]) -> list[dict]:
    """Resultados determinísticos já persistidos que cobrem estes arquivos.

    Serve para o subagente não repetir a medição que a máquina já fez. Se
    nenhuma task determinística terminou ainda, devolve lista vazia — o
    subagente segue sem esperar.
    """
    deterministic_ids = {
        task["task_id"]
        for task in plan.get("tasks", [])
        if task.get("kind", "deterministic") == "deterministic"
    }
    wanted = set(paths)
    parts: list[list[CheckResult]] = []
    for stored in repositories.load_review_tasks(plan_id):
        if stored["task_id"] not in deterministic_ids or stored["status"] != "done":
            continue
        part = []
        for check in stored["result"] or []:
            violations = [
                violation
                for violation in check.get("violations", [])
                if violation.get("file") in wanted
            ]
            part.append(CheckResult(**{**check, "violations": violations}))
        parts.append(part)

    # Uma fatia costuma ser coberta por mais de uma shard determinística; sem
    # agregar, o subagente receberia o mesmo check repetido por shard.
    return [
        check.model_dump(mode="json") for check in review_plan.aggregate_checks(parts)
    ]


CONTEXT_INSTRUCTIONS = (
    "Revise apenas os arquivos desta task. Use worktree_path para ler o entorno de uma "
    "mudança quando o diff não bastar, e standards como régua. Não repita o que já está em "
    "deterministic_checks. Devolva o resultado chamando submit_task_findings com a lista de "
    "findings no formato de finding_schema — lista vazia significa 'revisado e limpo'."
)

