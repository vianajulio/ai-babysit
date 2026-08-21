from app.gates.models import CheckResult, GateRun, GateStatus


def config_notes(workspace) -> list[str]:
    """Avisos sobre a execução, hoje só a ausência de config do projeto.

    Um relatório que não diz de onde vieram os limites obriga quem lê a
    adivinhar se `300 linhas` é regra do time ou default da ferramenta.
    """
    from settings import project_config_path

    if workspace is None or project_config_path(workspace) is not None:
        return []
    return [
        "sem .babysit.yml no projeto: usando os defaults globais do Babysit "
        "(crie o arquivo na raiz para definir os limites do time)"
    ]


def consolidate(run_id: str, pr_id: int, checks: list[CheckResult], **kwargs) -> GateRun:
    if any(c.status == GateStatus.failed for c in checks):
        overall = GateStatus.failed
    elif any(c.status == GateStatus.error for c in checks):
        overall = GateStatus.error
    elif any(c.status == GateStatus.warning for c in checks):
        overall = GateStatus.warning
    else:
        overall = GateStatus.passed

    return GateRun(run_id=run_id, pr_id=pr_id, status=overall, checks=checks, **kwargs)


def build_pr_comment(run: GateRun) -> str:
    status_emoji = "✅" if run.status == GateStatus.passed else "❌"
    lines = [f"## Quality Gate {status_emoji} {run.status.value.upper()}", ""]

    for check in run.checks:
        if check.status in (GateStatus.skipped, GateStatus.passed):
            lines.append(f"### {check.check} — {check.status.value}")
            continue

        lines.append(f"### {check.check} — {check.status.value}")

        for v in check.violations[:10]:
            loc = f"`{v.file}:{v.line}`" if v.line else f"`{v.file}`"
            lines.append(f"- {loc} — {v.message}")
            if v.current_value is not None and v.allowed_value is not None:
                lines.append(f"  - Atual: **{v.current_value}** | Limite: **{v.allowed_value}**")

        if check.suggestion:
            lines.append("")
            lines.append(f"> **Sugestão da IA:** {check.suggestion}")

        lines.append("")

    return "\n".join(lines)
