"""Construção das tasks de um plano de revisão.

Cada task carrega a chamada MCP seguinte já pronta (`call`, e `submit` nas
de agente), para o cliente fazer fan-out sem montar argumento nenhum.
"""
from __future__ import annotations

from app.gates.changed_file import ChangedFile


def review_task(group: list[ChangedFile], task_id: str, plan_id: str) -> dict:
    return {
        "task_id": task_id,
        "kind": "agent",
        "checks": ["agent_review"],
        "files": [f.path for f in group],
        "call": {
            "tool": "get_task_context",
            "args": {"plan_id": plan_id, "task_id": task_id},
        },
        "submit": {
            "tool": "submit_task_findings",
            "args": {"plan_id": plan_id, "task_id": task_id},
        },
    }


def one_shot_task(files: list[ChangedFile], workspace: str) -> dict:
    paths = [f.path for f in files]
    return {
        "task_id": "single",
        "kind": "deterministic",
        "checks": ["*"],
        "files": paths,
        "call": {
            "tool": "run_local_gate",
            "args": {"workspace": workspace, "files": paths},
        },
    }


def global_task(files: list[ChangedFile], checks: list[str], plan_id: str) -> dict:
    return {
        "task_id": "global",
        "kind": "deterministic",
        "checks": list(checks),
        "files": [f.path for f in files],
        "call": {
            "tool": "run_review_task",
            "args": {"plan_id": plan_id, "task_id": "global"},
        },
    }


def shard_task(shard: list[ChangedFile], checks: list[str], task_id: str, plan_id: str) -> dict:
    return {
        "task_id": task_id,
        "kind": "deterministic",
        "checks": list(checks),
        "files": [f.path for f in shard],
        "call": {
            "tool": "run_review_task",
            "args": {"plan_id": plan_id, "task_id": task_id},
        },
    }

