from typing import Any


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _trend(before: Any, after: Any) -> str:
    if before is None or after is None:
        return "sem baseline"
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return "-"
    if after > before:
        return "piorou"
    if after < before:
        return "melhorou"
    return "igual"


_MAX_LISTED_FINDINGS = 20
_FINDING_ORDER = {"high": 0, "medium": 1, "low": 2}


def _findings_section(run: dict) -> list[str]:
    """Lista os findings de `agent_review` abaixo da tabela.

    A tabela só sabe imprimir métrica numérica, então sem esta seção uma
    revisão semântica com dezenas de achados sairia como uma linha vazia. O
    corte em 20 evita devolver um markdown gigante ao contexto do agente
    principal — o restante continua no `GateRun`.
    """
    agent_checks = [check for check in run.get("checks", []) if check.get("check") == "agent_review"]
    if not agent_checks:
        return []

    lines: list[str] = []
    # Não filtrar por status: quando pelo menos uma task de agente respondeu, a
    # agregação eleva o check acima de `skipped` e o aviso sumiria justamente
    # no caso parcial. A métrica só existe quando houve task forçada.
    skipped = sum(
        int(check.get("metrics", {}).get("skipped_tasks", 0) or 0)
        for check in agent_checks
    )
    if skipped:
        lines.append("")
        lines.append(
            f"_agent_review: {skipped} task(s) sem retorno — revisão semântica incompleta._"
        )

    violations = [
        violation for check in agent_checks for violation in (check.get("violations") or [])
    ]
    if not violations:
        return lines

    ordered = sorted(
        violations,
        key=lambda violation: _FINDING_ORDER.get(str(violation.get("severity", "")).lower(), 3),
    )
    lines.append("")
    lines.append("### Findings (agent_review)")
    for violation in ordered[:_MAX_LISTED_FINDINGS]:
        location = violation.get("file", "?")
        line_number = violation.get("line")
        if line_number is not None:
            location = f"{location}:{line_number}"
        parts = [f"`{location}`", str(violation.get("severity", "")).lower()]
        if violation.get("category"):
            parts.append(violation["category"])
        parts.append(violation.get("message", ""))
        lines.append("- " + " — ".join(parts))
        if violation.get("suggestion"):
            lines.append(f"  Sugestão: {violation['suggestion']}")

    remaining = len(ordered) - _MAX_LISTED_FINDINGS
    if remaining > 0:
        run_id = run.get("run_id", "")
        lines.append(
            f'_(mais {remaining} findings; use `get_gate_run("{run_id}")` para a lista completa)_'
        )
    return lines


def build_quality_gate_table(run: dict, before_metrics: dict | None = None) -> str:
    before_metrics = before_metrics or {}
    lines = [
        "| Check | Status | Métrica | Antes | Depois | Resultado |",
        "|---|---|---|---:|---:|---|",
    ]

    checks = run.get("checks", [])
    if not checks:
        error = run.get("error", "sem checks retornados")
        lines.append(f"| quality_gate | error | error | - | {_format_value(error)} | - |")
        return "\n".join(lines)

    for check in checks:
        check_name = check.get("check", "-")
        status = check.get("status", "-")
        metrics = check.get("metrics") or {}

        numeric_metrics = {
            key: value
            for key, value in metrics.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }

        if not numeric_metrics:
            lines.append(f"| {check_name} | {status} | - | - | - | - |")
            continue

        for metric, after in numeric_metrics.items():
            before = before_metrics.get(f"{check_name}.{metric}", before_metrics.get(metric))
            lines.append(
                f"| {check_name} | {status} | {metric} | "
                f"{_format_value(before)} | {_format_value(after)} | {_trend(before, after)} |"
            )

    lines.extend(_findings_section(run))
    return "\n".join(lines)
