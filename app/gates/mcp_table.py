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

    return "\n".join(lines)
