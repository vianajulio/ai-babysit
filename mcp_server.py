import json
import os
from typing import Annotated

import httpx
from mcp.server.fastmcp import FastMCP

BABYSIT_URL = os.getenv("BABYSIT_URL", "http://localhost:8000")
TIMEOUT = 300.0

mcp = FastMCP("babysit")


def _err(msg: str, hint: str | None = None) -> str:
    payload: dict = {"error": msg}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload)


async def _get(path: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(f"{BABYSIT_URL}{path}")
            return r.text
    except httpx.ConnectError:
        return _err(
            "Não foi possível conectar ao servidor babysit.",
            f"Certifique-se de que o servidor está rodando em {BABYSIT_URL} (uvicorn main:app)",
        )
    except httpx.TimeoutException:
        return _err("Timeout ao chamar o servidor babysit.")


async def _post(path: str, payload: dict) -> str:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{BABYSIT_URL}{path}", json=payload)
            return r.text
    except httpx.ConnectError:
        return _err(
            "Não foi possível conectar ao servidor babysit.",
            f"Certifique-se de que o servidor está rodando em {BABYSIT_URL} (uvicorn main:app)",
        )
    except httpx.TimeoutException:
        return _err("Timeout ao chamar o servidor babysit.")


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
    return await _post(
        "/local/gate",
        {
            "workspace": workspace,
            "files": files,
            "repository": repository,
            "branch": branch,
            "use_ratchet": use_ratchet,
        },
    )


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
    return await _post(
        "/review",
        {
            "language": language,
            "filePath": file_path,
            "code": code,
            "diff": diff,
            "reviewMode": review_mode,
        },
    )


@mcp.tool()
async def get_gate_run(
    run_id: Annotated[str, "UUID da execução retornado por run_local_gate ou run_azure_pr_gate"],
) -> str:
    """Recupera o resultado completo de uma execução do quality gate."""
    return await _get(f"/gate-runs/{run_id}")


@mcp.tool()
async def get_gate_run_summary(
    run_id: Annotated[str, "UUID da execução"],
) -> str:
    """Recupera um resumo compacto de uma execução: status geral e quais checks falharam."""
    return await _get(f"/gate-runs/{run_id}/summary")


@mcp.tool()
async def list_azure_prs() -> str:
    """Lista os pull requests ativos no repositório Azure DevOps configurado."""
    return await _get("/providers/azure/prs")


@mcp.tool()
async def run_azure_pr_gate(
    pr_id: Annotated[int, "ID numérico do pull request no Azure DevOps"],
) -> str:
    """Executa o quality gate completo em um PR do Azure DevOps: clona o repositório, roda todos
    os checks, aplica ratchet e posta o resultado como comentário no PR."""
    return await _post(f"/providers/azure/prs/{pr_id}/gate", {})


if __name__ == "__main__":
    mcp.run()
