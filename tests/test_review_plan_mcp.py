import asyncio
import json
import subprocess

import pytest

import mcp_server_standalone as server
from app.gates.models import CheckResult, GateStatus
from app.storage import database, repositories


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isola cada teste em um SQLite próprio, sem tocar em babysit.db."""
    from sqlalchemy import create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(server, "_db_ready", False)
    database.init_db()
    return engine


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL
    ).stdout.strip()


def _init_repo(repo, *, force_sharded=False):
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")

    if force_sharded:
        (repo / ".babysit.yml").write_text(
            "quality_gate:\n"
            "  pr_review:\n"
            "    one_shot_max_files: 1\n"
            "    max_files_per_shard: 1\n",
            encoding="utf-8",
        )

    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "b.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


async def _fake_run_review_task_gate(workspace, changed_files, checks):
    return [
        CheckResult(check=name, status=GateStatus.passed, metrics={}, violations=[])
        for name in checks
    ]


def _plan_pr_review(repo, **kwargs):
    return json.loads(
        asyncio.run(
            server.plan_pr_review(str(repo), kwargs.pop("base_ref", "HEAD~1"), **kwargs)
        )
    )


def test_plan_pr_review_rejects_workspace_that_is_not_a_directory(tmp_path, db, monkeypatch):
    monkeypatch.setattr(server, "_ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    missing = tmp_path / "nope"

    result = json.loads(asyncio.run(server.plan_pr_review(str(missing), "HEAD~1")))

    assert "error" in result
    assert str(missing) in result["error"]


def test_plan_pr_review_rejects_workspace_that_is_not_a_git_repo(tmp_path, db, monkeypatch):
    monkeypatch.setattr(server, "_ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    not_repo = tmp_path / "plain"
    not_repo.mkdir()

    result = json.loads(asyncio.run(server.plan_pr_review(str(not_repo), "HEAD~1")))

    assert "error" in result


def test_plan_pr_review_returns_one_shot_for_small_diff(tmp_path, db):
    repo = _init_repo(tmp_path / "repo")

    plan = _plan_pr_review(repo)

    assert plan["mode"] == "one_shot"
    assert plan["files_total"] == 2

    tasks = repositories.load_review_tasks(plan["plan_id"])
    assert tasks == []


def test_run_review_task_rejects_unknown_plan_or_task(tmp_path, db, monkeypatch):
    monkeypatch.setattr(server, "run_review_task_gate", _fake_run_review_task_gate)

    unknown_plan = json.loads(asyncio.run(server.run_review_task("nope", "global")))
    assert "error" in unknown_plan

    repo = _init_repo(tmp_path / "repo", force_sharded=True)
    plan = _plan_pr_review(repo)
    assert plan["mode"] == "sharded"

    unknown_task = json.loads(asyncio.run(server.run_review_task(plan["plan_id"], "does-not-exist")))
    assert "error" in unknown_task


def test_run_review_task_is_idempotent_on_retry(tmp_path, db, monkeypatch):
    monkeypatch.setattr(server, "run_review_task_gate", _fake_run_review_task_gate)
    repo = _init_repo(tmp_path / "repo", force_sharded=True)
    plan = _plan_pr_review(repo)

    first = json.loads(asyncio.run(server.run_review_task(plan["plan_id"], "global")))
    second = json.loads(asyncio.run(server.run_review_task(plan["plan_id"], "global")))

    assert first["status"] == second["status"]
    assert first["remaining_tasks"] == second["remaining_tasks"]

    tasks = repositories.load_review_tasks(plan["plan_id"])
    global_tasks = [t for t in tasks if t["task_id"] == "global"]
    assert len(global_tasks) == 1


def test_get_review_plan_reports_pending_tasks_before_aggregating(tmp_path, db, monkeypatch):
    monkeypatch.setattr(server, "run_review_task_gate", _fake_run_review_task_gate)
    repo = _init_repo(tmp_path / "repo", force_sharded=True)
    plan = _plan_pr_review(repo)
    all_task_ids = {t["task_id"] for t in plan["tasks"]}
    assert len(all_task_ids) > 1

    status = json.loads(asyncio.run(server.get_review_plan(plan["plan_id"])))
    assert status["status"] == "pending"
    assert set(status["pending_tasks"]) == all_task_ids

    asyncio.run(server.run_review_task(plan["plan_id"], "global"))

    status = json.loads(asyncio.run(server.get_review_plan(plan["plan_id"])))
    assert status["status"] == "pending"
    assert "global" not in status["pending_tasks"]


def test_get_review_plan_returns_the_markdown_table_when_complete(tmp_path, db, monkeypatch):
    monkeypatch.setattr(server, "run_review_task_gate", _fake_run_review_task_gate)
    repo = _init_repo(tmp_path / "repo", force_sharded=True)
    plan = _plan_pr_review(repo)

    for task in plan["tasks"]:
        asyncio.run(server.run_review_task(plan["plan_id"], task["task_id"]))

    table = asyncio.run(server.get_review_plan(plan["plan_id"]))

    assert "|" in table
    assert "file_size" in table

    # Idempotente: chamada seguinte devolve a mesma tabela sem reprocessar.
    table_again = asyncio.run(server.get_review_plan(plan["plan_id"]))
    assert table_again == table

    close_result = json.loads(asyncio.run(server.close_review_plan(plan["plan_id"])))
    assert close_result["closed"] is True
    assert repositories.load_review_plan(plan["plan_id"]) is None
