"""Particionamento puro de uma revisão de PR em tasks one-shot ou sharded.

Função pura: não toca em git, disco ou banco — só decide como fatiar o
trabalho. A extração dos arquivos alterados (git) e a execução/persistência
das tasks vivem em outros módulos (`app/gates/git_diff.py`,
`app/gates/orchestrator.py`, `mcp_server_standalone.py`).
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.gates import ratchet, result
from app.gates.mcp_table import build_quality_gate_table
from app.gates.models import CheckResult, GateStatus
from app.storage import repositories

# Checks que rodam uma vez sobre a workspace inteira (não são shardáveis: o
# scan é global e a métrica também é). Ver tabela no topo do plano.
WORKSPACE_SCOPED: tuple[str, ...] = ("duplication", "secrets", "complexity")

# Checks que só olham para `changed_files` e por isso podem ser divididos
# em shards paralelos.
FILE_SCOPED: tuple[str, ...] = ("file_size", "big_o")

# Mesmos defaults usados por `orchestrator._build_runners`.
_DEFAULT_ENABLED: dict[str, bool] = {
    "file_size": True,
    "complexity": True,
    "duplication": True,
    "secrets": True,
    "big_o": False,
}


class ChangedFile(BaseModel):
    path: str
    added_lines: int = 0


def enabled_checks(config: dict, names: tuple[str, ...]) -> list[str]:
    """Filtra `names` respeitando `quality_gate.checks.<nome>.enabled`.

    Usa os mesmos defaults de `orchestrator._build_runners` quando a chave
    não está presente na config.
    """
    checks_cfg = config.get("quality_gate", {}).get("checks", {})
    return [
        name
        for name in names
        if checks_cfg.get(name, {}).get("enabled", _DEFAULT_ENABLED.get(name, True))
    ]


def _pr_review_config(config: dict) -> dict:
    return config.get("quality_gate", {}).get("pr_review", {})


def _pack_shards(
    files: list[ChangedFile],
    max_files_per_shard: int,
    max_diff_lines_per_shard: int,
) -> list[list[ChangedFile]]:
    """Greedy bin packing por `added_lines`, maior arquivo primeiro.

    Abre um novo shard quando o próximo arquivo estouraria o limite de
    arquivos ou de linhas do shard atual. Um único arquivo acima do limite
    ocupa um shard sozinho (nunca é dividido nem descartado).
    """
    ordered = sorted(files, key=lambda f: f.added_lines, reverse=True)
    shards: list[list[ChangedFile]] = []
    current: list[ChangedFile] = []
    current_lines = 0

    for changed_file in ordered:
        if not current:
            current = [changed_file]
            current_lines = changed_file.added_lines
            continue

        would_exceed_files = len(current) + 1 > max_files_per_shard
        would_exceed_lines = current_lines + changed_file.added_lines > max_diff_lines_per_shard
        if would_exceed_files or would_exceed_lines:
            shards.append(current)
            current = [changed_file]
            current_lines = changed_file.added_lines
        else:
            current.append(changed_file)
            current_lines += changed_file.added_lines

    if current:
        shards.append(current)

    return shards


def _one_shot_task(files: list[ChangedFile], plan_id: str) -> dict:
    paths = [f.path for f in files]
    return {
        "task_id": "single",
        "checks": ["*"],
        "files": paths,
        "call": {
            "tool": "run_local_gate",
            "args": {"plan_id": plan_id, "files": paths},
        },
    }


def _global_task(files: list[ChangedFile], checks: list[str], plan_id: str) -> dict:
    return {
        "task_id": "global",
        "checks": list(checks),
        "files": [f.path for f in files],
        "call": {
            "tool": "run_review_task",
            "args": {"plan_id": plan_id, "task_id": "global"},
        },
    }


def _shard_task(shard: list[ChangedFile], checks: list[str], task_id: str, plan_id: str) -> dict:
    return {
        "task_id": task_id,
        "checks": list(checks),
        "files": [f.path for f in shard],
        "call": {
            "tool": "run_review_task",
            "args": {"plan_id": plan_id, "task_id": task_id},
        },
    }


def build_plan(files: list[ChangedFile], config: dict, *, plan_id: str = "") -> dict:
    """Decide one_shot vs sharded e devolve as tasks já prontas para fan-out.

    Função pura: não toca em git, disco ou banco — só decide.
    """
    pr_cfg = _pr_review_config(config)
    one_shot_max_files = pr_cfg.get("one_shot_max_files", 10)
    one_shot_max_diff_lines = pr_cfg.get("one_shot_max_diff_lines", 800)
    max_files_per_shard = pr_cfg.get("max_files_per_shard", 15)
    max_diff_lines_per_shard = pr_cfg.get("max_diff_lines_per_shard", 1200)
    parallel_hint = pr_cfg.get("parallel_hint", 4)

    files_total = len(files)
    total_lines = sum(f.added_lines for f in files)

    def one_shot_plan(reason: str) -> dict:
        return {
            "plan_id": plan_id,
            "mode": "one_shot",
            "files_total": files_total,
            "reason": reason,
            "parallel_hint": parallel_hint,
            "tasks": [_one_shot_task(files, plan_id)],
        }

    fits_one_shot = (
        files_total <= one_shot_max_files and total_lines <= one_shot_max_diff_lines
    )
    if fits_one_shot:
        reason = (
            f"{files_total} arquivos / {total_lines} linhas dentro do limite "
            f"one-shot ({one_shot_max_files} / {one_shot_max_diff_lines})"
        )
        return one_shot_plan(reason)

    workspace_checks = enabled_checks(config, WORKSPACE_SCOPED)
    file_checks = enabled_checks(config, FILE_SCOPED)

    tasks: list[dict] = []
    if workspace_checks:
        tasks.append(_global_task(files, workspace_checks, plan_id))

    if file_checks:
        shards = _pack_shards(files, max_files_per_shard, max_diff_lines_per_shard)
        for index, shard in enumerate(shards, start=1):
            task_id = f"files-{index}"
            tasks.append(_shard_task(shard, file_checks, task_id, plan_id))

    if not tasks:
        # Nenhum check habilitado para agendar: nada a shardar, devolve
        # one_shot para que o cliente ainda tenha uma chamada a fazer.
        return one_shot_plan("nenhum check habilitado para agendamento shardado")

    reason = (
        f"{files_total} arquivos / {total_lines} linhas acima do limite "
        f"one-shot ({one_shot_max_files} / {one_shot_max_diff_lines})"
    )
    return {
        "plan_id": plan_id,
        "mode": "sharded",
        "files_total": files_total,
        "reason": reason,
        "parallel_hint": parallel_hint,
        "tasks": tasks,
    }


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


def _worst_status(statuses: list[GateStatus]) -> GateStatus:
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
            merged[key] = values[0]

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
                status=_worst_status([check.status for check in group]),
                language=group[0].language,
                metrics=_merge_metrics(group),
                violations=_merge_violations(group),
            )
        )
    return aggregated


async def finalize_plan(
    plan: dict,
    parts: list[list[CheckResult]],
    *,
    config: dict,
    repository: str = "local",
    branch: str = "local",
    use_ratchet: bool = False,
    run_id: str = "",
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

    if use_ratchet and gate_run.status == GateStatus.passed:
        current_metrics = ratchet.extract_metrics(checks)
        merged = {**before_metrics, **current_metrics} if before_metrics else current_metrics
        repositories.save_baseline(repository, branch, merged)

    repositories.save_gate_run(gate_run)
    return build_quality_gate_table(gate_run.model_dump(mode="json"), before_metrics)
