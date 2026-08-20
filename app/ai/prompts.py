from pathlib import Path

from settings import settings


# Fonte única do formato de finding devolvido por um subagente revisor.
# Consumido pelo plano de revisão (`plan_pr_review`) e por `get_task_context`,
# para que a régua seja a mesma em qualquer cliente MCP.
FINDING_SCHEMA: dict[str, str] = {
    "file": "caminho relativo à raiz revisada",
    "line": "int ou null",
    "severity": "high | medium | low",
    "category": "correctness | security | performance | design | test | style",
    "message": "o problema, uma frase",
    "suggestion": "a correção concreta",
}


def load_standards(workspace: Path | None = None) -> str:
    """Lê o padrão de código do projeto revisado.

    Resolve `settings.standards_path` dentro de `workspace` quando informado —
    o servidor MCP roda de um checkout separado, então o caminho relativo ao
    CWD apontaria para o padrão do próprio Babysit, não o do projeto.
    """
    if workspace is not None:
        candidate = Path(workspace) / settings.standards_path
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")

    path = Path(settings.standards_path)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def build_review_prompt(standards: str, language: str, file_path: str, code: str, diff: str | None) -> str:
    diff_section = f"\n[DIFF]\n{diff}" if diff else ""
    return f"""Você é um revisor de código especializado em C#, .NET, Clean Architecture, SOLID, EF Core e APIs REST.

Use obrigatoriamente o padrão de código abaixo:

[PADRÃO DE CÓDIGO]
{standards}

Analise o código abaixo.

Regras:
- Não reescreva o arquivo inteiro.
- Aponte apenas problemas reais.
- Classifique cada problema como low, medium ou high.
- Sugira correções práticas.
- Verifique nomenclatura, responsabilidades, arquitetura, async/await, validações, exceptions e logs.
- Responda APENAS com JSON válido, sem texto adicional fora do JSON.

Contexto:
- language: {language}
- filePath: {file_path}

[CÓDIGO]
{code}
{diff_section}

Responda APENAS com o seguinte JSON válido:
{{
  "summary": "resumo geral do código",
  "severity": "low|medium|high",
  "issues": [
    {{
      "type": "architecture|naming|async|validation|exception|log|solid",
      "severity": "low|medium|high",
      "line": 0,
      "problem": "descrição do problema",
      "suggestion": "sugestão de correção"
    }}
  ],
  "approved": true
}}"""


def build_big_o_prompt(language: str, file_path: str, code: str) -> str:
    return f"""Voce e um revisor especializado em complexidade algoritmica e Big O.

Analise somente riscos de complexidade algoritmica. Ignore estilo, arquitetura, nomenclatura, logs e validacoes, a menos que afetem diretamente Big O.

Classificacao:
- high: risco claro de escalabilidade, como consulta a banco dentro de loop, varredura quadratica evitavel em colecoes grandes, ordenacao/materializacao repetida dentro de loop.
- medium: ineficiencia provavel, mas dependente de contexto ou tamanho dos dados.
- low: oportunidade menor que nao deve afetar o quality gate.

Contexto:
- language: {language}
- filePath: {file_path}

[CODIGO]
{code}

Responda APENAS com JSON valido, sem texto adicional fora do JSON:
{{
  "summary": "resumo curto",
  "issues": [
    {{
      "severity": "low|medium|high",
      "line": 0,
      "current_complexity": "O(n^2)",
      "suggested_complexity": "O(n)",
      "problem": "descricao do problema",
      "suggestion": "correcao pratica"
    }}
  ]
}}"""


def build_suggestion_prompt(check_name: str, violations: list[dict]) -> str:
    violations_text = "\n".join(
        f"- {v.get('file', '')}:{v.get('line', '')} — {v.get('message', '')}"
        for v in violations
    )
    return f"""Você é um especialista em qualidade de código.

O check "{check_name}" encontrou as seguintes violações:

{violations_text}

Forneça sugestões práticas e concisas de como corrigir esses problemas.
Responda em português, em formato de texto simples (sem JSON), com no máximo 5 linhas.
"""
