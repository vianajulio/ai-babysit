"""Particionamento puro de uma revisão de PR em tasks one-shot ou sharded.

Função pura: não toca em git, disco ou banco — só decide como fatiar o
trabalho. A extração dos arquivos alterados (git) e a execução/persistência
das tasks vivem em outros módulos (`app/gates/git_diff.py`,
`app/gates/orchestrator.py`, `mcp_server_standalone.py`).
"""
from __future__ import annotations

from app.ai.prompts import FINDING_SCHEMA
from app.gates.changed_file import ChangedFile
from app.gates.review_tasks import global_task, one_shot_task, review_task, shard_task

# Checks que rodam uma vez sobre a workspace inteira (não são shardáveis: o
# scan é global e a métrica também é). Ver tabela no topo do plano.
WORKSPACE_SCOPED: tuple[str, ...] = ("duplication", "secrets", "complexity")

# Checks que só olham para `changed_files` e por isso podem ser divididos
# em shards paralelos.
FILE_SCOPED: tuple[str, ...] = ("file_size", "big_o")

# Check produzido fora do servidor: um subagente do cliente lê o contexto
# (`get_task_context`), julga com o próprio LLM e devolve findings por
# `submit_task_findings`.
AGENT_SCOPED: tuple[str, ...] = ("agent_review",)

MODES: tuple[str, ...] = ("auto", "inline", "sharded")

# Mesmos defaults usados por `orchestrator._build_runners`.
_DEFAULT_ENABLED: dict[str, bool] = {
    "file_size": True,
    "complexity": True,
    "duplication": True,
    "secrets": True,
    "big_o": False,
    "agent_review": False,
}


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


def _pack_review_tasks(
    files: list[ChangedFile],
    max_files_per_task: int,
    max_diff_lines_per_task: int,
) -> list[list[ChangedFile]]:
    """Agrupa por diretório e só então empacota, com tetos próprios.

    Diferente de `_pack_shards` (que otimiza balanceamento de custo de
    ferramenta), aqui o objetivo é coerência: um subagente revisa melhor seis
    arquivos do mesmo módulo do que seis arquivos sem relação com o mesmo peso
    somado. Por isso nenhum grupo de diretório é misturado com outro, mesmo que
    isso deixe tasks pequenas.
    """
    by_directory: dict[str, list[ChangedFile]] = {}
    for changed_file in files:
        directory = changed_file.path.rsplit("/", 1)[0] if "/" in changed_file.path else ""
        by_directory.setdefault(directory, []).append(changed_file)

    packed: list[list[ChangedFile]] = []
    for directory in sorted(by_directory):
        group = sorted(by_directory[directory], key=lambda f: f.added_lines, reverse=True)
        packed.extend(_pack_shards(group, max_files_per_task, max_diff_lines_per_task))
    return packed



def build_plan(
    files: list[ChangedFile],
    config: dict,
    *,
    plan_id: str = "",
    workspace: str = "",
    standards: str = "",
    mode: str = "auto",
) -> dict:
    """Decide one_shot vs sharded e devolve as tasks já prontas para fan-out.

    Função pura: não toca em git, disco ou banco — só decide. `workspace` entra
    nos argumentos da call one-shot (que roda `run_local_gate` no checkout do
    projeto) e `standards` viaja no plano para o cliente revisar com a mesma
    régua nos dois modos.
    """
    if mode not in MODES:
        raise ValueError(f"mode inválido: {mode!r}; use um de {', '.join(MODES)}")

    pr_cfg = _pr_review_config(config)
    one_shot_max_files = pr_cfg.get("one_shot_max_files", 10)
    one_shot_max_diff_lines = pr_cfg.get("one_shot_max_diff_lines", 800)
    max_files_per_shard = pr_cfg.get("max_files_per_shard", 15)
    max_diff_lines_per_shard = pr_cfg.get("max_diff_lines_per_shard", 1200)
    max_files_per_review_task = pr_cfg.get("max_files_per_review_task", 6)
    max_diff_lines_per_review_task = pr_cfg.get("max_diff_lines_per_review_task", 400)
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
            "tasks": [one_shot_task(files, workspace)],
            "standards": standards,
            "finding_schema": dict(FINDING_SCHEMA),
        }

    if mode == "inline":
        return one_shot_plan("modo inline forçado pelo cliente")

    fits_one_shot = mode != "sharded" and (
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
        tasks.append(global_task(files, workspace_checks, plan_id))

    if file_checks:
        shards = _pack_shards(files, max_files_per_shard, max_diff_lines_per_shard)
        for index, shard in enumerate(shards, start=1):
            task_id = f"files-{index}"
            tasks.append(shard_task(shard, file_checks, task_id, plan_id))

    if enabled_checks(config, AGENT_SCOPED):
        groups = _pack_review_tasks(
            files, max_files_per_review_task, max_diff_lines_per_review_task
        )
        for index, group in enumerate(groups, start=1):
            tasks.append(review_task(group, f"review-{index}", plan_id))

    if not tasks:
        # Nenhum check habilitado para agendar: nada a shardar, devolve
        # one_shot para que o cliente ainda tenha uma chamada a fazer.
        return one_shot_plan("nenhum check habilitado para agendamento shardado")

    reason = (
        "fan-out forçado pelo cliente"
        if mode == "sharded"
        else (
            f"{files_total} arquivos / {total_lines} linhas acima do limite "
            f"one-shot ({one_shot_max_files} / {one_shot_max_diff_lines})"
        )
    )
    return {
        "plan_id": plan_id,
        "mode": "sharded",
        "files_total": files_total,
        "reason": reason,
        "parallel_hint": parallel_hint,
        "tasks": tasks,
        "standards": standards,
        "finding_schema": dict(FINDING_SCHEMA),
    }


# Reexportado para não quebrar quem importa de `review_plan`; a
# implementação mora nos módulos abaixo desde que este arquivo passou do
# limite de tamanho do próprio gate.
from app.gates.agent_findings import findings_to_check_result  # noqa: E402
from app.gates.review_aggregate import (  # noqa: E402
    aggregate_checks,
    baseline_metrics,
    finalize_plan,
    worst_status,
)

__all__ = [
    "AGENT_SCOPED",
    "ChangedFile",
    "FILE_SCOPED",
    "MODES",
    "WORKSPACE_SCOPED",
    "aggregate_checks",
    "baseline_metrics",
    "build_plan",
    "enabled_checks",
    "finalize_plan",
    "findings_to_check_result",
    "worst_status",
]
