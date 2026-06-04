"""
MCP server standalone — importa o orchestrator diretamente, sem precisar
que o servidor HTTP (uvicorn) esteja rodando.
"""
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated

# Garante que o projeto esteja no sys.path independente de onde o servidor é iniciado
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP

from app.ai import ollama
from app.ai.prompts import build_review_prompt, load_standards
from app.gates.mcp_table import build_quality_gate_table
from app.gates.orchestrator import run_local_quality_gate, run_quality_gate
from app.providers import azure_devops
from app.storage import database, repositories
from settings import settings


mcp = FastMCP("babysit-standalone")

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


def _http_err(exc: HTTPException) -> str:
    return json.dumps({"error": exc.detail, "status_code": exc.status_code})


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
        )
        detected = out.stdout.strip()
        if out.returncode == 0 and detected and detected != "HEAD":
            return detected
    except Exception:
        pass
    return "local"


@mcp.tool()
async def run_local_gate(
    workspace: Annotated[str, "Caminho absoluto do diretório raiz do projeto"],
    files: Annotated[list[str], "Lista de arquivos a analisar (relativos ao workspace ou absolutos)"],
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
        # Espelha a resolução dos runners (workspace / file.lstrip("/")) para abortar
        # com mensagem clara em vez de gerar um gate "verde" sobre arquivos inexistentes.
        missing = [f for f in files if not (ws / f.lstrip("/")).exists()]
        if missing:
            return _exc_err(ValueError(f"arquivos não encontrados no workspace: {missing}"))

        _ensure_db()
        repository = _auto_repository(workspace, repository)
        branch = _auto_branch(workspace, branch)
        before_metrics = repositories.load_baseline(repository, branch) if use_ratchet else None
        result = await run_local_quality_gate(
            workspace=Path(workspace),
            changed_files=files,
            repository=repository,
            branch=branch,
            use_ratchet=use_ratchet,
        )
        return build_quality_gate_table(result, before_metrics)
    except HTTPException as exc:
        return _http_err(exc)
    except Exception as exc:
        return _exc_err(exc)


@mcp.tool()
async def review_file(
    language: Annotated[str, "Linguagem do arquivo (ex: csharp, python, typescript)"],
    file_path: Annotated[str, "Caminho do arquivo (usado como contexto no prompt)"],
    code: Annotated[str, "Conteúdo completo do arquivo"],
    diff: Annotated[str | None, "Diff git do arquivo para focar a revisão nas mudanças"] = None,
    review_mode: Annotated[str, "Modo de revisão: strict, suggest_only ou lenient"] = "strict",
) -> str:
    """Analisa um arquivo via IA (Ollama) e retorna issues com severidade, linha, problema e
    sugestão de correção. Use após run_local_gate para investigar violações específicas."""
    try:
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
    except HTTPException as exc:
        return _http_err(exc)
    except Exception as exc:
        return _exc_err(exc)


@mcp.tool()
async def get_gate_run(
    run_id: Annotated[str, "UUID da execução retornado por run_local_gate ou run_azure_pr_gate"],
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
async def list_azure_prs() -> str:
    """Lista os pull requests ativos no repositório Azure DevOps configurado."""
    try:
        prs = await azure_devops.list_pull_requests()
        return json.dumps(prs)
    except HTTPException as exc:
        return _http_err(exc)
    except Exception as exc:
        return _exc_err(exc)


@mcp.tool()
async def run_azure_pr_gate(
    pr_id: Annotated[int, "ID numérico do pull request no Azure DevOps"],
) -> str:
    """Executa o quality gate completo em um PR do Azure DevOps: clona o repositório, roda todos
    os checks, aplica ratchet e posta o resultado como comentário no PR."""
    try:
        _ensure_db()
        pr = await azure_devops.get_pull_request(pr_id)
        target_branch = pr["targetRefName"].replace("refs/heads/", "")
        before_metrics = repositories.load_baseline(settings.azure_repo, target_branch)
        result = await run_quality_gate(pr_id)
        return build_quality_gate_table(result, before_metrics)
    except HTTPException as exc:
        return _http_err(exc)
    except Exception as exc:
        return _exc_err(exc)


if __name__ == "__main__":
    mcp.run()
