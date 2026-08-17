"""
MCP server standalone — importa o orchestrator diretamente, sem precisar
que um servidor HTTP esteja rodando.
"""
import json
import ntpath
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from sqlalchemy import delete, select

# Garante que o projeto esteja no sys.path independente de onde o servidor é iniciado
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.gates import git_diff, review_plan
from app.gates.mcp_table import build_quality_gate_table
from app.gates.models import CheckResult
from app.gates.orchestrator import run_local_quality_gate, run_review_task_gate
from app.storage import database, repositories
from app.storage.database import ReviewPlanRecord, ReviewTaskRecord
from settings import settings


class _StandaloneMCP:
    """Defers the MCP runtime import until the server is actually started.

    Importing this module is enough for local tool calls and must not pull in
    an HTTP server runtime.  The real FastMCP instance is built only by the
    command-line entry point.
    """

    def __init__(self, name: str):
        self.name = name
        self._tools = []

    def tool(self):
        def register(function):
            self._tools.append(function)
            return function

        return register

    def run(self):
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as exc:  # pragma: no cover - depends on installation
            raise RuntimeError("o pacote mcp é necessário para executar o servidor") from exc

        server = FastMCP(self.name)
        for function in self._tools:
            server.tool()(function)
        server.run()


mcp = _StandaloneMCP("babysit-standalone")

_db_ready = False


def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    url = settings.babysit_database_url
    if url.startswith("sqlite:///"):
        db_path = Path(url.removeprefix("sqlite:///"))
        if db_path.is_absolute():
            db_path.parent.mkdir(parents=True, exist_ok=True)
    database.init_db()
    _db_ready = True


def _exc_err(exc: Exception) -> str:
    return json.dumps({"error": str(exc)})


def _auto_repository(workspace: str, repository: str) -> str:
    """Deriva o identificador do repositório a partir do caminho relativo enxuto
    do workspace (dois últimos componentes, ex.: ``energia/backend``) quando o
    chamador não informa um valor explícito. Evita que projetos diferentes
    colidam na mesma linha de baseline ao compartilhar o mesmo banco."""
    if repository and repository != "local":
        return repository
    parts = Path(workspace).resolve().parts
    relative = "/".join(parts[-2:]).lstrip("/")
    return relative or "local"


def _auto_branch(workspace: str, branch: str) -> str:
    """Deriva a branch atual via git quando não informada explicitamente."""
    if branch and branch != "local":
        return branch
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(workspace).resolve()), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        detected = out.stdout.strip()
        if out.returncode == 0 and detected and detected != "HEAD":
            return detected
    except Exception:
        pass
    return "local"


def _resolve_local_files(workspace: Path, files: list[str]) -> list[str]:
    """Resolve input files and return safe workspace-relative paths.

    A leading slash is retained for compatibility with the runners' old
    ``lstrip('/')`` behavior when it does not identify an existing external
    file.  Existing absolute paths outside the workspace are always rejected.
    """
    root = workspace.resolve()
    resolved_files: list[str] = []
    missing: list[str] = []
    outside: list[str] = []

    for file_name in files:
        text = str(file_name)
        candidate = Path(text)
        resolved: Path

        rooted_path = candidate.is_absolute() or text.startswith(("/", "\\"))
        if rooted_path:
            absolute_candidate = candidate.resolve()
            if absolute_candidate.exists():
                resolved = absolute_candidate
            elif ntpath.splitdrive(text)[0] or text.startswith("\\\\"):
                missing.append(text)
                continue
            else:
                # ``/Foo.py`` has historically meant ``workspace/Foo.py`` in
                # the local MCP API.  Only use that compatibility form when
                # the actual absolute path does not exist.
                resolved = (root / text.lstrip("/\\")).resolve()
        else:
            resolved = (root / candidate).resolve()

        try:
            relative = resolved.relative_to(root)
        except ValueError:
            outside.append(text)
            continue

        if not resolved.exists():
            missing.append(relative.as_posix())
        elif not resolved.is_file():
            missing.append(relative.as_posix())
        else:
            resolved_files.append(relative.as_posix())

    if outside:
        raise ValueError(f"arquivos fora do workspace: {outside}")
    if missing:
        raise ValueError(f"arquivos não encontrados no workspace: {missing}")
    return resolved_files


# `run_git` mora em app/gates/git_diff.py; mantido aqui sob o nome antigo
# porque os demais usos neste arquivo (worktree add/remove) ainda chamam `_git`.
_git = git_diff.run_git


def _commit_files(repo_root: Path, sha: str, base_commit: str | None) -> tuple[str, list[str]]:
    if not sha or not sha.strip():
        raise ValueError("SHA do commit é obrigatório")

    resolved_sha = git_diff.resolve_commit(repo_root, sha)
    resolved_base = git_diff.resolve_commit(repo_root, base_commit if base_commit else f"{resolved_sha}^")

    changed_files = [
        file.path for file in git_diff.changed_files_with_weight(repo_root, resolved_base, resolved_sha)
    ]
    return resolved_sha, changed_files


def _cleanup_worktree(repo_root: Path, worktree: Path | None, temp_root: Path | None) -> None:
    if worktree is not None:
        try:
            _git(["worktree", "remove", "--force", str(worktree)], repo_root, allow_failure=True)
        except Exception:
            # Filesystem cleanup below is still required if Git itself is
            # unavailable or cannot unregister a partially-created worktree.
            pass
    if temp_root is not None:
        shutil.rmtree(temp_root, ignore_errors=True)


def _cleanup_plan_runtime(runtime: dict) -> None:
    """Libera o worktree/diretório temporário guardados em `payload.runtime`."""
    repo_root = runtime.get("repo_root")
    worktree = runtime.get("worktree")
    temp_root = runtime.get("temp_root")
    if repo_root and worktree:
        _cleanup_worktree(Path(repo_root), Path(worktree), Path(temp_root) if temp_root else None)
    elif temp_root:
        shutil.rmtree(temp_root, ignore_errors=True)


def _delete_review_plan_records(plan_id: str) -> None:
    with database.get_session() as session:
        session.execute(delete(ReviewTaskRecord).where(ReviewTaskRecord.plan_id == plan_id))
        session.execute(delete(ReviewPlanRecord).where(ReviewPlanRecord.plan_id == plan_id))
        session.commit()


def _expire_stale_plans(ttl_minutes: int) -> None:
    """Limpa planos pendentes mais velhos que `plan_ttl_minutes`, liberando worktrees
    órfãos deixados por clientes que nunca chamaram `close_review_plan`."""
    cutoff = datetime.utcnow() - timedelta(minutes=max(ttl_minutes, 0))
    with database.get_session() as session:
        stale = session.scalars(
            select(ReviewPlanRecord).where(
                ReviewPlanRecord.status == "pending",
                ReviewPlanRecord.created_at < cutoff,
            )
        ).all()
        for record in stale:
            try:
                payload = json.loads(record.payload_json)
                _cleanup_plan_runtime(payload.get("runtime", {}))
            except Exception:
                pass
            session.execute(delete(ReviewTaskRecord).where(ReviewTaskRecord.plan_id == record.plan_id))
            session.delete(record)
        session.commit()


@mcp.tool()
async def run_local_gate(
    workspace: Annotated[str, "Caminho do diretório raiz do projeto"],
    files: Annotated[list[str], "Lista de arquivos a analisar, relativos ao workspace"],
    repository: Annotated[str, "Identificador do repositório para baseline. Deixe vazio para derivar automaticamente do caminho relativo do workspace"] = "",
    branch: Annotated[str, "Nome da branch. Deixe vazio para detectar automaticamente via git no workspace"] = "",
    use_ratchet: Annotated[bool, "Se true, falha se métricas regredirem em relação ao baseline anterior"] = False,
) -> str:
    """Executa o quality gate em arquivos locais: verifica tamanho, complexidade ciclomática,
    duplicação de código e secrets expostos. Use sempre que gerar ou modificar arquivos de código.
    Não preencha repository/branch: eles são derivados do workspace para evitar colisão entre projetos."""
    try:
        ws = Path(workspace).resolve()
        if not ws.is_dir():
            return _exc_err(ValueError(f"workspace não é um diretório: {workspace}"))
        if not files:
            return _exc_err(ValueError("nenhum arquivo informado para análise"))
        changed_files = _resolve_local_files(ws, files)

        _ensure_db()
        repository = _auto_repository(workspace, repository)
        branch = _auto_branch(workspace, branch)
        before_metrics = repositories.load_baseline(repository, branch) if use_ratchet else None
        result = await run_local_quality_gate(
            workspace=ws,
            changed_files=changed_files,
            repository=repository,
            branch=branch,
            use_ratchet=use_ratchet,
        )
        return build_quality_gate_table(result, before_metrics)
    except Exception as exc:
        return _exc_err(exc)


@mcp.tool()
async def run_commit_gate(
    workspace: Annotated[str, "Diretório de um repositório Git local"],
    sha: Annotated[str, "SHA ou referência do commit a analisar"],
    base_commit: Annotated[str | None, "Commit base opcional; por padrão usa o pai do SHA"] = None,
    repository: Annotated[str, "Identificador opcional para baseline"] = "",
    branch: Annotated[str, "Nome opcional da referência para baseline"] = "",
    use_ratchet: Annotated[bool, "Se true, aplica o baseline; false por padrão"] = False,
) -> str:
    """Roda o gate de um commit em um worktree detached temporário.

    O checkout atual do usuário nunca é trocado. Apenas arquivos adicionados,
    modificados, renomeados ou com mudança de tipo são analisados; deleções
    ficam fora da lista. O worktree e seus metadados são removidos sempre.
    """
    repo_root: Path | None = None
    worktree: Path | None = None
    temp_root: Path | None = None
    added = False
    try:
        requested_workspace = Path(workspace).resolve()
        if not requested_workspace.is_dir():
            raise ValueError(f"workspace não é um diretório: {workspace}")

        repo_root = Path(_git(["rev-parse", "--show-toplevel"], requested_workspace).strip()).resolve()
        resolved_sha, changed_files = _commit_files(repo_root, sha, base_commit)
        if not changed_files:
            raise ValueError("o commit não possui arquivos alterados não-deletados")

        temp_root = Path(tempfile.mkdtemp(prefix="babysit-commit-gate-"))
        worktree = temp_root / "repo"
        _git(["worktree", "add", "--detach", str(worktree), resolved_sha], repo_root)
        added = True

        _ensure_db()
        selected_repository = repository or _auto_repository(str(repo_root), "")
        selected_branch = branch or f"commit-{resolved_sha[:12]}"
        before_metrics = (
            repositories.load_baseline(selected_repository, selected_branch)
            if use_ratchet
            else None
        )
        result = await run_local_quality_gate(
            workspace=worktree,
            changed_files=changed_files,
            repository=selected_repository,
            branch=selected_branch,
            use_ratchet=use_ratchet,
        )
        return build_quality_gate_table(result, before_metrics)
    except Exception as exc:
        return _exc_err(exc)
    finally:
        if repo_root is not None:
            _cleanup_worktree(repo_root, worktree if added else None, temp_root)
        elif temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


@mcp.tool()
async def review_file(
    language: Annotated[str, "Linguagem do arquivo (ex: csharp, python, typescript)"],
    file_path: Annotated[str, "Caminho do arquivo (usado como contexto no prompt)"],
    code: Annotated[str, "Conteúdo completo do arquivo"],
    diff: Annotated[str | None, "Diff git do arquivo para focar a revisão nas mudanças"] = None,
    review_mode: Annotated[str, "Modo de revisão: strict, suggest_only ou lenient"] = "strict",
) -> str:
    """Analisa um arquivo via IA (Ollama) e retorna issues com severidade, linha, problema e
    sugestão de correção. É opcional e retorna um erro claro se Ollama não estiver disponível."""
    try:
        # AI is an explicit opt-in MCP operation and is not imported for local
        # quality gates that do not request it.
        from app.ai import ollama
        from app.ai.prompts import build_review_prompt, load_standards

        standards = load_standards()
        prompt = build_review_prompt(
            standards=standards,
            language=language,
            file_path=file_path,
            code=code,
            diff=diff,
        )
        result = await ollama.generate_json(prompt)
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"error": f"revisão IA indisponível: {exc}"})


@mcp.tool()
async def get_gate_run(
    run_id: Annotated[str, "UUID da execução retornado por um gate local"],
) -> str:
    """Recupera o resultado completo de uma execução do quality gate."""
    try:
        _ensure_db()
        data = repositories.get_gate_run(run_id)
        if data is None:
            return json.dumps({"error": f"Execução '{run_id}' não encontrada"})
        return json.dumps(data)
    except Exception as exc:
        return _exc_err(exc)


@mcp.tool()
async def get_gate_run_summary(
    run_id: Annotated[str, "UUID da execução"],
) -> str:
    """Recupera um resumo compacto de uma execução: status geral e quais checks falharam."""
    try:
        _ensure_db()
        data = repositories.get_gate_run(run_id)
        if data is None:
            return json.dumps({"error": f"Execução '{run_id}' não encontrada"})

        failed = [c["check"] for c in data.get("checks", []) if c.get("status") == "failed"]
        summary = {
            "run_id": run_id,
            "status": data.get("status"),
            "total_checks": len(data.get("checks", [])),
            "failed_checks": len(failed),
            "failed": failed,
        }
        return json.dumps(summary)
    except Exception as exc:
        return _exc_err(exc)


@mcp.tool()
async def plan_pr_review(
    workspace: Annotated[str, "Raiz do repositório Git a revisar"],
    base_ref: Annotated[str, "Referência base do PR (ex.: origin/main)"],
    head_ref: Annotated[str, "Referência de topo; padrão HEAD"] = "HEAD",
    repository: Annotated[str, "Identificador opcional para baseline"] = "",
    branch: Annotated[str, "Branch opcional para baseline"] = "",
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
            repo_root = Path(_git(["rev-parse", "--show-toplevel"], ws).strip()).resolve()
        except RuntimeError as exc:
            raise ValueError(f"workspace não é um repositório git: {workspace}") from exc

        _ensure_db()
        config = settings.load_quality_gate_config(repo_root)
        pr_review_cfg = config.get("quality_gate", {}).get("pr_review", {})
        _expire_stale_plans(pr_review_cfg.get("plan_ttl_minutes", 60))

        resolved_base = git_diff.resolve_commit(repo_root, base_ref)
        resolved_head = git_diff.resolve_commit(repo_root, head_ref)
        changed_files = git_diff.changed_files_with_weight(repo_root, resolved_base, resolved_head)
        if not changed_files:
            raise ValueError("nenhum arquivo alterado entre as referências")

        plan_id = str(uuid.uuid4())
        plan = review_plan.build_plan(files=changed_files, config=config, plan_id=plan_id)

        selected_repository = _auto_repository(str(repo_root), repository)
        selected_branch = branch or _auto_branch(str(repo_root), "")

        if plan["mode"] == "sharded":
            # Uma revisão sobre commit/branch que não é o checkout atual usa um único
            # worktree detached (como run_commit_gate já faz); todas as tasks o reusam
            # para não multiplicar custo criando um worktree por task.
            temp_root = Path(tempfile.mkdtemp(prefix="babysit-review-plan-"))
            worktree = temp_root / "repo"
            _git(["worktree", "add", "--detach", str(worktree), resolved_head], repo_root)
            added = True

        payload = {
            "plan": plan,
            "runtime": {
                "repo_root": str(repo_root),
                "worktree": str(worktree) if worktree else None,
                "temp_root": str(temp_root) if temp_root else None,
                "config": config,
                "repository": selected_repository,
                "branch": selected_branch,
                "use_ratchet": False,
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
            _cleanup_worktree(repo_root, worktree if added else None, temp_root)
        elif temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
        return _exc_err(exc)


@mcp.tool()
async def run_review_task(
    plan_id: Annotated[str, "ID do plano retornado por plan_pr_review"],
    task_id: Annotated[str, "ID da task a executar (ex.: 'global', 'files-1')"],
) -> str:
    """Executa somente os checks de uma task do plano de revisão (sobre um subconjunto dos
    arquivos alterados) e persiste o resultado parcial por (plan_id, task_id) — repetir a
    chamada para a mesma task é seguro e apenas sobrescreve o resultado anterior. Devolve um
    resumo enxuto; a tabela consolidada só sai de get_review_plan, depois que todas as tasks
    do plano estiverem concluídas."""
    try:
        _ensure_db()
        record = repositories.load_review_plan(plan_id)
        if record is None:
            raise ValueError(f"plano '{plan_id}' não encontrado")

        plan = record["payload"].get("plan", {})
        runtime = record["payload"].get("runtime", {})
        tasks_by_id = {task["task_id"]: task for task in plan.get("tasks", [])}
        task = tasks_by_id.get(task_id)
        if task is None:
            raise ValueError(f"task '{task_id}' não encontrada no plano '{plan_id}'")

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

        return json.dumps({
            "plan_id": plan_id,
            "task_id": task_id,
            "status": worst,
            "violations": violations,
            "remaining_tasks": remaining,
        })
    except Exception as exc:
        return _exc_err(exc)


@mcp.tool()
async def get_review_plan(
    plan_id: Annotated[str, "ID do plano retornado por plan_pr_review"],
) -> str:
    """Devolve {"status": "pending", "pending_tasks": [...]} enquanto faltar task, ou a
    tabela markdown consolidada quando todas as tasks estiverem concluídas — mesmo formato
    de saída de run_local_gate/run_commit_gate. Ratchet e baseline só são aplicados aqui,
    uma única vez sobre o resultado agregado. Chamadas repetidas depois de completo devolvem
    a mesma tabela, sem reprocessar."""
    try:
        _ensure_db()
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

        if pending:
            return json.dumps({"status": "pending", "pending_tasks": pending})

        parts = [
            [CheckResult(**check) for check in (done[task_id]["result"] or [])]
            for task_id in all_ids
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
        return _exc_err(exc)


@mcp.tool()
async def close_review_plan(
    plan_id: Annotated[str, "ID do plano a encerrar"],
) -> str:
    """Remove o worktree e os registros de um plano de revisão. Chame sempre ao final do
    fluxo — mesmo que o cliente não conclua todas as tasks — para não deixar worktree órfão."""
    try:
        _ensure_db()
        record = repositories.load_review_plan(plan_id)
        if record is None:
            raise ValueError(f"plano '{plan_id}' não encontrado")

        _cleanup_plan_runtime(record["payload"].get("runtime", {}))
        _delete_review_plan_records(plan_id)
        return json.dumps({"plan_id": plan_id, "closed": True})
    except Exception as exc:
        return _exc_err(exc)


if __name__ == "__main__":
    mcp.run()
