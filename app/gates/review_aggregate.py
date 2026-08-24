"""Agregação dos resultados parciais de um plano de revisão em um veredito.

`aggregate_checks` é pura (só funde listas em memória); `finalize_plan` toca
baseline e banco — é o único lugar que aplica ratchet e salva o `GateRun` de
um plano.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from app.gates import ratchet, result
from app.gates.mcp_table import build_quality_gate_table
from app.gates.models import CheckResult, GateStatus
from app.storage import repositories


# --- Agregação dos resultados parciais -------------------------------------
#
# As funções abaixo já não são puras: `finalize_plan` toca baseline e banco
# (é o único lugar que aplica ratchet e salva o `GateRun` de um plano — ver
# Task 4, que deixa `run_review_task_gate` explicitamente sem esses efeitos
# colaterais). `aggregate_checks` continua pura: só funde listas em memória.

_STATUS_SEVERITY: dict[GateStatus, int] = {
    GateStatus.skipped: 0,
    GateStatus.passed: 1,
    GateStatus.warning: 2,
    GateStatus.failed: 3,
    GateStatus.error: 4,
}

# Sufixos de métricas que devem ser somadas entre partes (contadores). Tudo
# que não bate com isso nem começa com "max_" é tratado como valor global
# (percentual/média/mensagem de erro) e mantém o primeiro valor não nulo.
_SUM_METRIC_SUFFIXES = ("count", "_found", "_issues")


def worst_status(statuses: list[GateStatus]) -> GateStatus:
    return max(statuses, key=lambda status: _STATUS_SEVERITY[status])


def _merge_violations(group: list[CheckResult]) -> list:
    """Concatenação estável (ordem de task, depois ordem original) sem duplicatas."""
    seen: set[tuple] = set()
    merged = []
    for check in group:
        for violation in check.violations:
            key = (violation.file, violation.line, violation.message)
            if key in seen:
                continue
            seen.add(key)
            merged.append(violation)
    return merged


def _merge_metrics(group: list[CheckResult]) -> dict:
    ordered_keys: list[str] = []
    for check in group:
        for key in check.metrics:
            if key not in ordered_keys:
                ordered_keys.append(key)

    merged: dict = {}
    for key in ordered_keys:
        values = [check.metrics[key] for check in group if key in check.metrics]
        numeric_values = [
            value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]

        if key.startswith("max_") and numeric_values:
            merged[key] = max(numeric_values)
        elif key.endswith(_SUM_METRIC_SUFFIXES) and numeric_values:
            merged[key] = sum(numeric_values)
        else:
            # duplication_percent e afins vêm de uma única task (a global);
            # mensagens de erro também não devem ser somadas nem "maximizadas".
            # Primeiro valor não nulo: um shard que abortou traz a chave vazia e
            # descartaria o valor real de outra parte.
            merged[key] = next((value for value in values if value is not None), values[0])

    return merged


def aggregate_checks(parts: list[list[CheckResult]]) -> list[CheckResult]:
    """Funde os resultados parciais de várias tasks em um único `list[CheckResult]`.

    Agrupa por nome do check (`CheckResult.check`), na ordem em que cada
    check aparece pela primeira vez entre as partes. Por grupo:

    - violations: concatenação estável (ordem de task, depois ordem
      original), deduplicada por `(file, line, message)`;
    - métricas: prefixo `max_*` -> maior valor; sufixo
      `count`/`_found`/`_issues` -> soma; qualquer outra (percentuais,
      médias globais como `duplication_percent`, mensagens de erro) ->
      primeiro valor não nulo encontrado;
    - status: o pior entre as partes, na ordem
      `error > failed > warning > passed > skipped` — um `skipped` de um
      shard nunca rebaixa um `passed` de outro.
    """
    grouped: dict[str, list[CheckResult]] = {}
    order: list[str] = []
    for part in parts:
        for check in part:
            if check.check not in grouped:
                grouped[check.check] = []
                order.append(check.check)
            grouped[check.check].append(check)

    aggregated: list[CheckResult] = []
    for name in order:
        group = grouped[name]
        aggregated.append(
            CheckResult(
                check=name,
                status=worst_status([check.status for check in group]),
                language=group[0].language,
                metrics=_merge_metrics(group),
                violations=_merge_violations(group),
            )
        )
    return aggregated


def baseline_metrics(checks: list[CheckResult]) -> dict:
    """Métricas do run que podem virar baseline.

    Tudo que veio de `agent_review` fica de fora: findings de subagente não são
    reprodutíveis entre execuções, e gravá-los como baseline faria o ratchet
    oscilar (o mesmo diff reprovaria ou passaria conforme a rodada). Eles
    continuam no `GateRun` — o que muda é só o que é promovido a referência.
    """
    return {
        key: value
        for key, value in ratchet.extract_metrics(checks).items()
        if not key.startswith("agent_review.")
    }


def _skipped_checks(forced_tasks: list[dict]) -> list[CheckResult]:
    """Marcadores para as tasks que não retornaram, um por check afetado.

    Uma task de agente que não voltou é revisão semântica faltando; uma task
    determinística que não voltou é *medição* faltando, e some da tabela se
    não for anunciada — quem lê acharia que `duplication` simplesmente passou.
    Por isso cada família recebe seu próprio marcador em vez de tudo virar
    `agent_review`.
    """
    agent_count = 0
    deterministic: dict[str, int] = {}
    for task in forced_tasks:
        if task.get("kind", "deterministic") == "agent":
            agent_count += 1
            continue
        for check in task.get("checks", []):
            if check == "*":
                continue
            deterministic[check] = deterministic.get(check, 0) + 1

    markers = [
        CheckResult(check=name, status=GateStatus.skipped, metrics={"skipped_tasks": count})
        for name, count in deterministic.items()
    ]
    if agent_count:
        markers.append(CheckResult(
            check="agent_review",
            status=GateStatus.skipped,
            metrics={"skipped_tasks": agent_count},
        ))
    return markers


async def finalize_plan(
    plan: dict,
    parts: list[list[CheckResult]],
    *,
    config: dict,
    repository: str = "local",
    branch: str = "local",
    use_ratchet: bool = False,
    run_id: str = "",
    forced_tasks: list[dict] | None = None,
    workspace: "Path | None" = None,
) -> str:
    """Fecha um plano de revisão: agrega, aplica ratchet/IA uma única vez, persiste.

    `aggregate_checks` funde todas as partes primeiro; ratchet e anotação de
    IA rodam exatamente uma vez sobre o resultado já agregado (nunca por
    shard — aplicar em resultado parcial gravaria um baseline/veredito
    derivado de uma fatia do PR). Salva baseline só quando `use_ratchet` está
    ligado e o resultado consolidado passou, e sempre persiste o `GateRun`.
    Devolve a mesma tabela markdown que `run_local_gate` já produz hoje, para
    o cliente não precisar aprender um segundo formato.
    """
    # Import tardio: evita puxar os runners (e a dependência opcional do
    # Ollama) para quem só usa a parte pura deste módulo (`build_plan`).
    from app.gates.orchestrator import _apply_ai_annotation

    run_id = run_id or plan.get("plan_id") or str(uuid.uuid4())
    forced_tasks = list(forced_tasks or [])
    if forced_tasks:
        parts = [*parts, _skipped_checks(forced_tasks)]
    checks = aggregate_checks(parts)

    before_metrics = repositories.load_baseline(repository, branch) if use_ratchet else None
    checks = ratchet.apply(checks, before_metrics)
    checks = await _apply_ai_annotation(checks, config)

    gate_run = result.consolidate(
        run_id=run_id,
        pr_id=0,
        checks=checks,
        repository=repository,
        source_branch=branch,
        target_branch=branch,
    )

    gate_run = gate_run.model_copy(update={"notes": result.config_notes(workspace)})

    if forced_tasks and gate_run.status == GateStatus.passed:
        # Consolidar sem a revisão semântica que foi pedida não pode devolver um
        # "passou" limpo: o cliente leria como PR revisado quando não foi.
        gate_run = gate_run.model_copy(update={"status": GateStatus.warning})

    if use_ratchet and not forced_tasks and gate_run.status == GateStatus.passed:
        current_metrics = baseline_metrics(checks)
        merged = {**before_metrics, **current_metrics} if before_metrics else current_metrics
        repositories.save_baseline(repository, branch, merged)

    repositories.save_gate_run(gate_run)
    return build_quality_gate_table(gate_run.model_dump(mode="json"), before_metrics)
