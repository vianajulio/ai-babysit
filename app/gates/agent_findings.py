"""Conversão dos findings de um subagente revisor em `CheckResult`.

Separado de `review_plan` (particionamento) e de `review_aggregate`
(consolidação) porque é a única parte que fala o vocabulário do subagente:
`high|medium|low` em vez do `GateStatus` interno.
"""
from __future__ import annotations

from app.gates.models import CheckResult, GateStatus, Violation
from app.gates.review_aggregate import worst_status


# --- Findings de subagente --------------------------------------------------

# Vocabulário do subagente (semântico) mapeado para o enum interno do gate.
# `low` é informativo: aparece na saída, não muda o veredito.
_FINDING_SEVERITY: dict[str, GateStatus] = {
    "high": GateStatus.failed,
    "medium": GateStatus.warning,
    "low": GateStatus.passed,
}


def findings_to_check_result(findings: list[dict], block_on: str = "high") -> CheckResult:
    """Converte findings de um subagente em um `CheckResult` do check `agent_review`.

    Função pura. Valida o vocabulário de severidade em vez de aceitar qualquer
    string: um finding com severidade inventada viraria um veredito silencioso.
    Com `block_on != "high"`, o status é limitado a `warning` — o time vê a
    review sem reprovar o build. Lista vazia é um `passed` legítimo: significa
    "revisado e limpo", que é diferente de "não revisado" (esse caso vira
    `skipped` na agregação).
    """
    counts = {"high": 0, "medium": 0, "low": 0}
    violations: list[Violation] = []

    for index, finding in enumerate(findings):
        severity = str(finding.get("severity", "")).strip().lower()
        if severity not in _FINDING_SEVERITY:
            raise ValueError(
                f"finding {index}: severity inválida {finding.get('severity')!r}; "
                f"use uma de {', '.join(_FINDING_SEVERITY)}"
            )
        file_path = str(finding.get("file", "")).strip()
        message = str(finding.get("message", "")).strip()
        if not file_path:
            raise ValueError(f"finding {index}: 'file' é obrigatório")
        if not message:
            raise ValueError(f"finding {index}: 'message' é obrigatório")

        line = finding.get("line")
        counts[severity] += 1
        violations.append(Violation(
            file=file_path,
            line=int(line) if isinstance(line, (int, float)) and not isinstance(line, bool) else None,
            severity=severity,
            message=message,
            category=str(finding.get("category", "") or ""),
            suggestion=str(finding.get("suggestion", "") or ""),
        ))

    status = worst_status([_FINDING_SEVERITY[v.severity] for v in violations] or [GateStatus.passed])
    if block_on != "high" and status == GateStatus.failed:
        status = GateStatus.warning

    return CheckResult(
        check="agent_review",
        status=status,
        metrics={
            "high_issues": counts["high"],
            "medium_issues": counts["medium"],
            "low_issues": counts["low"],
            "findings_count": len(violations),
        },
        violations=violations,
    )


