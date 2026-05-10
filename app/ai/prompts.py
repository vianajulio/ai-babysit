from pathlib import Path

from settings import settings


def load_standards() -> str:
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
