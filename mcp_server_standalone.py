"""
MCP server standalone — importa o orchestrator diretamente, sem precisar
que o servidor HTTP (uvicorn) esteja rodando.
"""
import json
import sys
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def lifespan(_app):
    database.init_db()
    yield


mcp = FastMCP("babysit-standalone", lifespan=lifespan)


def _http_err(exc: HTTPException) -> str:
    return json.dumps({"error": exc.detail, "status_code": exc.status_code})


def _exc_err(exc: Exception) -> str:
    return json.dumps({"error": str(exc)})


@mcp.tool()
async def run_local_gate(
    workspace: Annotated[str, "Caminho absoluto do diretório raiz do projeto"],
    files: Annotated[list[str], "Lista de arquivos a analisar (relativos ao workspace ou absolutos)"],
    repository: Annotated[str, "Identificador do repositório para rastreamento de baseline"] = "local",
    branch: Annotated[str, "Nome da branch atual"] = "local",
    use_ratchet: Annotated[bool, "Se true, falha se métricas regredirem em relação ao baseline anterior"] = False,
) -> str:
    """Executa o quality gate em arquivos locais: verifica tamanho, complexidade ciclomática,
    duplicação de código e secrets expostos. Use sempre que gerar ou modificar arquivos de código."""
    try:
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
