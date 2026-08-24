"""Escritas concorrentes de subagentes e PRAGMAs do SQLite."""
import asyncio
import json
import subprocess

import pytest

import mcp_server_standalone as server
from app.mcp import gate_tools, review_tools, runtime
from app.gates.models import CheckResult, GateStatus
from app.gates.review_plan import findings_to_check_result
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


def _plan(repo):
    return json.loads(asyncio.run(server.plan_pr_review(str(repo), "HEAD~1")))


def _repo_many_modules(tmp_path, modules=6):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / ".babysit.yml").write_text(
        "quality_gate:\n"
        "  checks:\n"
        "    agent_review:\n"
        "      enabled: true\n"
        "  pr_review:\n"
        "    one_shot_max_files: 1\n"
        "    max_files_per_shard: 50\n"
        "    max_files_per_review_task: 1\n",
        encoding="utf-8",
    )
    for index in range(modules):
        module = repo / f"mod{index}"
        module.mkdir()
        (module / "x.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    for index in range(modules):
        (repo / f"mod{index}" / "x.py").write_text(f"value = {index + 2}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


def test_parallel_task_writes_do_not_lock_the_database(tmp_path, db):
    plan = _plan(_repo_many_modules(tmp_path))
    agent_tasks = [t["task_id"] for t in plan["tasks"] if t.get("kind") == "agent"]
    assert len(agent_tasks) >= 4

    async def _write_all():
        return await asyncio.gather(*[
            server.submit_task_findings(
                plan["plan_id"], task_id,
                [{"file": f"mod{index}/x.py", "severity": "low", "message": "detalhe"}],
            )
            for index, task_id in enumerate(agent_tasks)
        ])

    results = asyncio.run(_write_all())

    assert all("error" not in json.loads(result) for result in results)


def test_sqlite_engine_enables_wal(tmp_path):
    from sqlalchemy import text

    engine = database.create_sqlite_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000
