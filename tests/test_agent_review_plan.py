from app.gates import review_plan
from app.gates.review_plan import ChangedFile


def _file(path: str, added: int = 50) -> ChangedFile:
    return ChangedFile(path=path, added_lines=added)


def _files(n: int, added: int = 50, prefix: str = "app/gates") -> list[ChangedFile]:
    return [_file(f"{prefix}/file{i}.py", added) for i in range(n)]


def _cfg(agent_review: bool = False, block_on: str = "high") -> dict:
    return {
        "quality_gate": {
            "checks": {
                "file_size": {"enabled": True},
                "complexity": {"enabled": True},
                "duplication": {"enabled": True},
                "secrets": {"enabled": True},
                "big_o": {"enabled": False},
                "agent_review": {"enabled": agent_review, "block_on": block_on},
            },
            "pr_review": {
                "one_shot_max_files": 10,
                "one_shot_max_diff_lines": 800,
                "max_files_per_shard": 15,
                "max_diff_lines_per_shard": 1200,
                "parallel_hint": 4,
                "max_context_chars": 120000,
                "max_files_per_review_task": 6,
                "max_diff_lines_per_review_task": 400,
            },
        }
    }


def _agent_tasks(plan: dict) -> list[dict]:
    return [task for task in plan["tasks"] if task.get("kind") == "agent"]


def test_mode_inline_forces_one_shot_on_a_big_pr():
    plan = review_plan.build_plan(files=_files(63), config=_cfg(), mode="inline")

    assert plan["mode"] == "one_shot"
    assert len(plan["tasks"]) == 1


def test_mode_sharded_forces_fan_out_on_a_small_pr():
    plan = review_plan.build_plan(files=_files(3, added=10), config=_cfg(), mode="sharded")

    assert plan["mode"] == "sharded"


def test_unknown_mode_is_rejected():
    import pytest

    with pytest.raises(ValueError, match="auto"):
        review_plan.build_plan(files=_files(3), config=_cfg(), mode="turbo")


def test_agent_review_disabled_emits_no_review_tasks():
    plan = review_plan.build_plan(files=_files(40), config=_cfg(), mode="sharded")

    assert _agent_tasks(plan) == []
    assert all(task["kind"] == "deterministic" for task in plan["tasks"])


def test_review_tasks_group_by_directory_before_packing():
    files = [
        _file("app/gates/a.py"), _file("web/ui/b.tsx"),
        _file("app/gates/c.py"), _file("web/ui/d.tsx"),
    ]
    tasks = _agent_tasks(
        review_plan.build_plan(files=files, config=_cfg(agent_review=True), mode="sharded")
    )

    assert tasks
    for task in tasks:
        directories = {path.rsplit("/", 1)[0] for path in task["files"]}
        assert len(directories) == 1


def test_review_tasks_respect_their_own_smaller_caps():
    files = _files(30, added=100)
    plan = review_plan.build_plan(files=files, config=_cfg(agent_review=True), mode="sharded")
    tasks = _agent_tasks(plan)
    weights = {f.path: f.added_lines for f in files}

    assert all(len(t["files"]) <= 6 for t in tasks)
    assert all(
        sum(weights[p] for p in t["files"]) <= 400 or len(t["files"]) == 1 for t in tasks
    )
    assert sum(len(t["files"]) for t in tasks) == 30
    assert len({p for t in tasks for p in t["files"]}) == 30


def test_a_single_huge_file_gets_its_own_review_task():
    files = [_file("app/gates/huge.py", 4000), *_files(4, added=10)]
    tasks = _agent_tasks(
        review_plan.build_plan(files=files, config=_cfg(agent_review=True), mode="sharded")
    )

    assert ["app/gates/huge.py"] in [t["files"] for t in tasks]


def test_review_task_declares_both_calls():
    plan = review_plan.build_plan(
        files=_files(8, added=100), config=_cfg(agent_review=True), plan_id="p1", mode="sharded"
    )
    task = _agent_tasks(plan)[0]

    assert task["call"]["tool"] == "get_task_context"
    assert task["call"]["args"] == {"plan_id": "p1", "task_id": task["task_id"]}
    assert task["submit"]["tool"] == "submit_task_findings"
    assert task["checks"] == ["agent_review"]
    assert task["task_id"].startswith("review-")


def test_one_shot_never_emits_review_tasks():
    plan = review_plan.build_plan(files=_files(3, added=10), config=_cfg(agent_review=True))

    assert plan["mode"] == "one_shot"
    assert _agent_tasks(plan) == []
    assert plan["finding_schema"]["severity"] == "high | medium | low"
