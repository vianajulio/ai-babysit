import asyncio
import json
import subprocess

import pytest

import mcp_server_standalone as server
from app.mcp import gate_tools, review_tools, runtime
from app.storage import database, repositories


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(runtime, "_db_ready", False)
    database.init_db()
    return engine


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _babysit_yml(max_context_chars: int = 120000) -> str:
    return (
        "quality_gate:\n"
        "  checks:\n"
        "    agent_review:\n"
        "      enabled: true\n"
        "  pr_review:\n"
        "    one_shot_max_files: 1\n"
        "    max_files_per_shard: 1\n"
        "    max_files_per_review_task: 2\n"
        "    max_diff_lines_per_review_task: 400\n"
        f"    max_context_chars: {max_context_chars}\n"
    )


def _repo_with_agent_plan(tmp_path, *, big: bool = False, max_context_chars: int = 120000):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")

    (repo / ".babysit.yml").write_text(_babysit_yml(max_context_chars), encoding="utf-8")
    (repo / "app" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "app" / "c.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    body = "\n".join(f"linha_{i} = {i}" for i in range(400 if big else 3))
    (repo / "app" / "a.py").write_text(f"value = 2\n{body}\n", encoding="utf-8")
    (repo / "app" / "c.py").write_text(f"value = 3\n{body}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


def _plan(repo, **kwargs):
    return json.loads(
        asyncio.run(server.plan_pr_review(str(repo), kwargs.pop("base_ref", "HEAD~1"), **kwargs))
    )


def _context(plan_id, task_id="review-1"):
    return json.loads(asyncio.run(server.get_task_context(plan_id, task_id)))


def _agent_task_id(plan):
    return next(task["task_id"] for task in plan["tasks"] if task.get("kind") == "agent")


def test_context_returns_diff_per_file_with_worktree_path(tmp_path, db):
    plan = _plan(_repo_with_agent_plan(tmp_path))
    ctx = _context(plan["plan_id"], _agent_task_id(plan))

    assert ctx["worktree_path"]
    assert [f["path"] for f in ctx["files"]] == ["app/a.py", "app/c.py"]
    assert "@@" in ctx["files"][0]["diff"]
    assert ctx["files"][0]["added_lines"] > 0
    assert ctx["truncated"] == []
    assert ctx["volatile"] is False


def test_context_carries_standards_and_finding_schema(tmp_path, db):
    repo = _repo_with_agent_plan(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "coding-standards.md").write_text("REGRAS", encoding="utf-8")
    plan = _plan(repo)

    ctx = _context(plan["plan_id"], _agent_task_id(plan))

    assert ctx["standards"] == "REGRAS"
    assert set(ctx["finding_schema"]) == {
        "file", "line", "severity", "category", "message", "suggestion",
    }
    assert "submit_task_findings" in ctx["instructions"]


def test_context_truncates_loudly_when_over_the_char_budget(tmp_path, db):
    plan = _plan(_repo_with_agent_plan(tmp_path, big=True, max_context_chars=500))
    ctx = _context(plan["plan_id"], _agent_task_id(plan))

    assert ctx["truncated"]
    assert all(f["path"] in ctx["truncated"] or f["diff"] for f in ctx["files"])
    assert sum(len(f["diff"]) for f in ctx["files"]) <= 500


def test_context_includes_deterministic_results_already_computed(tmp_path, db):
    plan = _plan(_repo_with_agent_plan(tmp_path))
    deterministic = next(
        task["task_id"] for task in plan["tasks"] if task.get("kind") == "deterministic"
    )
    asyncio.run(server.run_review_task(plan["plan_id"], deterministic))

    ctx = _context(plan["plan_id"], _agent_task_id(plan))
    checks = {check["check"] for check in ctx["deterministic_checks"]}

    assert checks


def test_context_is_empty_of_checks_before_any_deterministic_task_runs(tmp_path, db):
    plan = _plan(_repo_with_agent_plan(tmp_path))

    ctx = _context(plan["plan_id"], _agent_task_id(plan))

    assert ctx["deterministic_checks"] == []


def test_context_rejects_a_deterministic_task(tmp_path, db):
    plan = _plan(_repo_with_agent_plan(tmp_path))
    deterministic = next(
        task["task_id"] for task in plan["tasks"] if task.get("kind") == "deterministic"
    )

    out = _context(plan["plan_id"], deterministic)

    assert "error" in out
    assert "run_review_task" in out["error"]


def test_context_rejects_unknown_plan_or_task(tmp_path, db):
    plan = _plan(_repo_with_agent_plan(tmp_path))

    assert "error" in _context("nope", "review-1")
    assert "error" in _context(plan["plan_id"], "review-999")


def test_worktree_mode_context_reads_the_live_checkout(tmp_path, db):
    repo = _repo_with_agent_plan(tmp_path)
    (repo / "app" / "novo.py").write_text("print('x')\n", encoding="utf-8")

    plan = _plan(repo, base_ref="HEAD", head_ref="WORKTREE", mode="sharded")
    ctx = _context(plan["plan_id"], _agent_task_id(plan))

    assert ctx["volatile"] is True
    assert ctx["worktree_path"] == str(repo.resolve())
    diffs = {f["path"]: f["diff"] for f in ctx["files"]}
    assert "app/novo.py" in diffs
    assert "print('x')" in diffs["app/novo.py"]      # untracked vira diff sintético


def test_saved_plan_records_the_refs_used_for_the_diff(tmp_path, db):
    plan = _plan(_repo_with_agent_plan(tmp_path))
    runtime = repositories.load_review_plan(plan["plan_id"])["payload"]["runtime"]

    assert runtime["base"]
    assert runtime["head"]


def test_deterministic_checks_are_aggregated_not_repeated_per_shard(tmp_path, db):
    plan = _plan(_repo_with_agent_plan(tmp_path))
    for task in plan["tasks"]:
        if task.get("kind", "deterministic") == "deterministic":
            asyncio.run(server.run_review_task(plan["plan_id"], task["task_id"]))

    ctx = _context(plan["plan_id"], _agent_task_id(plan))
    names = [check["check"] for check in ctx["deterministic_checks"]]

    assert len(names) == len(set(names))
