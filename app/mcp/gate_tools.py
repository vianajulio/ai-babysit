"""Tools de gate direto: arquivos locais, commit e consultas de execução."""
import json
import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from app.gates.mcp_table import build_quality_gate_table
from app.gates.orchestrator import run_local_quality_gate
from app.mcp.runtime import ensure_db, exc_err, mcp
from app.mcp.workspace import (
    auto_branch,
    auto_repository,
    cleanup_worktree,
    commit_files,
    git,
    resolve_local_files,
)
from app.storage import repositories


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
            return exc_err(ValueError(f"workspace não é um diretório: {workspace}"))
        if not files:
            return exc_err(ValueError("nenhum arquivo informado para análise"))
        changed_files = resolve_local_files(ws, files)

        ensure_db()
        repository = auto_repository(workspace, repository)
        branch = auto_branch(workspace, branch)
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
        return exc_err(exc)


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

        repo_root = Path(git(["rev-parse", "--show-toplevel"], requested_workspace).strip()).resolve()
        resolved_sha, changed_files = commit_files(repo_root, sha, base_commit)
        if not changed_files:
            raise ValueError("o commit não possui arquivos alterados não-deletados")

        temp_root = Path(tempfile.mkdtemp(prefix="babysit-commit-gate-"))
        worktree = temp_root / "repo"
        git(["worktree", "add", "--detach", str(worktree), resolved_sha], repo_root)
        added = True

        ensure_db()
        selected_repository = repository or auto_repository(str(repo_root), "")
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
        return exc_err(exc)
    finally:
        if repo_root is not None:
            cleanup_worktree(repo_root, worktree if added else None, temp_root)
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
        ensure_db()
        data = repositories.get_gate_run(run_id)
        if data is None:
            return json.dumps({"error": f"Execução '{run_id}' não encontrada"})
        return json.dumps(data)
    except Exception as exc:
        return exc_err(exc)


@mcp.tool()
async def get_gate_run_summary(
    run_id: Annotated[str, "UUID da execução"],
) -> str:
    """Recupera um resumo compacto de uma execução: status geral e quais checks falharam."""
    try:
        ensure_db()
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
        return exc_err(exc)

