from app.gates.models import CheckResult, GateStatus

_RATCHET_METRICS = {
    "duplication_percent": "higher_is_worse",
    "average_complexity": "higher_is_worse",
    "max_complexity": "higher_is_worse",
    "max_file_lines": "higher_is_worse",
    "max_function_lines": "higher_is_worse",
    "violations_count": "higher_is_worse",
    "secrets_found": "higher_is_worse",
    "high_issues": "higher_is_worse",
    "medium_issues": "higher_is_worse",
}


def extract_metrics(checks: list[CheckResult]) -> dict:
    combined: dict = {}
    for check in checks:
        for key, value in check.metrics.items():
            if isinstance(value, (int, float)):
                combined[f"{check.check}.{key}"] = value
    return combined


def compare(current: dict, baseline: dict) -> dict[str, bool]:
    """Returns {metric: True if worsened} for ratcheted metrics."""
    regressions = {}
    for key, direction in _RATCHET_METRICS.items():
        for prefix in ("", *[f"{c}." for c in ("file_size", "complexity", "duplication", "secrets", "big_o")]):
            fqk = f"{prefix}{key}" if prefix else key
            if fqk in current and fqk in baseline:
                cur = current[fqk]
                base = baseline[fqk]
                if direction == "higher_is_worse":
                    regressions[fqk] = cur > base
    return regressions


def apply(checks: list[CheckResult], baseline: dict | None) -> list[CheckResult]:
    """Mark checks as failed when they worsen baseline metrics. Returns updated checks."""
    if not baseline:
        return checks

    current = extract_metrics(checks)
    regressions = compare(current, baseline)

    updated = []
    for check in checks:
        check_regressions = {k: v for k, v in regressions.items() if k.startswith(f"{check.check}.")}
        worsened = any(check_regressions.values())
        if worsened and check.status == GateStatus.passed:
            check = check.model_copy(update={"status": GateStatus.failed})
        elif check.status == GateStatus.failed and check_regressions and not worsened:
            check = check.model_copy(update={"status": GateStatus.passed})
        updated.append(check)

    return updated
