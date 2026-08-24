from app.gates import review_plan
from app.gates.review_plan import ChangedFile


def _file(path: str, added: int) -> ChangedFile:
    return ChangedFile(path=path, added_lines=added)


def _files(n: int, added: int) -> list[ChangedFile]:
    return [_file(f"file{i}.py", added) for i in range(n)]


def _cfg(checks: dict | None = None) -> dict:
    base_checks = {
        "file_size": {"enabled": True},
        "complexity": {"enabled": True},
        "duplication": {"enabled": True},
        "secrets": {"enabled": True},
        "big_o": {"enabled": False},
    }
    if checks:
        for name, override in checks.items():
            base_checks.setdefault(name, {}).update(override)

    return {
        "quality_gate": {
            "checks": base_checks,
            "pr_review": {
                "one_shot_max_files": 10,
                "one_shot_max_diff_lines": 800,
                "max_files_per_shard": 15,
                "max_diff_lines_per_shard": 1200,
                "parallel_hint": 4,
            },
        }
    }


def _shards(plan: dict) -> list[dict]:
    return [t for t in plan["tasks"] if t["task_id"] != "global"]


def test_small_pr_becomes_one_shot():
    plan = review_plan.build_plan(files=_files(4, added=30), config=_cfg())

    assert plan["mode"] == "one_shot"
    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["call"]["tool"] == "run_local_gate"


def test_big_pr_splits_into_global_task_plus_file_shards():
    plan = review_plan.build_plan(files=_files(63, added=60), config=_cfg())

    assert plan["mode"] == "sharded"
    assert plan["tasks"][0]["task_id"] == "global"
    assert plan["tasks"][0]["checks"] == ["duplication", "secrets", "complexity"]
    assert len(plan["tasks"][0]["files"]) == 63          # escopo global recebe tudo
    shards = plan["tasks"][1:]
    assert all(len(t["files"]) <= 15 for t in shards)
    assert sum(len(t["files"]) for t in shards) == 63    # cobertura total, sem overlap


def test_shards_balance_by_added_lines_not_only_by_count():
    files = [_file("huge.cs", 4000), *_files(10, added=5)]
    shards = _shards(review_plan.build_plan(files=files, config=_cfg()))

    assert ["huge.cs"] == shards[0]["files"]             # arquivo pesado isolado


def test_one_shot_when_few_files_but_forced_sharded_by_diff_lines():
    plan = review_plan.build_plan(files=_files(6, added=400), config=_cfg())
    assert plan["mode"] == "sharded"                     # 2400 linhas > one_shot_max_diff_lines


def test_disabled_checks_are_not_scheduled():
    cfg = _cfg(checks={"duplication": {"enabled": False}, "big_o": {"enabled": False}})
    plan = review_plan.build_plan(files=_files(40, added=10), config=cfg)

    assert "duplication" not in plan["tasks"][0]["checks"]
    assert all("big_o" not in t["checks"] for t in plan["tasks"])


def test_one_shot_call_targets_run_local_gate_with_workspace():
    plan = review_plan.build_plan(
        files=_files(4, added=30), config=_cfg(), plan_id="p1", workspace="/tmp/projetos/x"
    )
    call = plan["tasks"][0]["call"]

    assert call["tool"] == "run_local_gate"
    assert call["args"]["workspace"] == "/tmp/projetos/x"
    assert set(call["args"]) == {"workspace", "files"}


def test_one_shot_plan_carries_standards_and_schema():
    plan = review_plan.build_plan(
        files=_files(4, added=30), config=_cfg(), plan_id="p1",
        workspace="/tmp/x", standards="PADRÃO",
    )

    assert plan["standards"] == "PADRÃO"
    assert plan["finding_schema"]["severity"] == "high | medium | low"
