"""Fluxo completo da camada de subagente pelas tools MCP."""
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


def _repo(tmp_path, block_on="high"):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / ".babysit.yml").write_text(
        "quality_gate:\n"
        "  checks:\n"
        "    agent_review:\n"
        "      enabled: true\n"
        f"      block_on: {block_on}\n"
        "  pr_review:\n"
        "    one_shot_max_files: 1\n"
        "    max_files_per_shard: 1\n"
        "    max_files_per_review_task: 2\n",
        encoding="utf-8",
    )
    (repo / "app" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "app" / "c.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "app" / "a.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "app" / "c.py").write_text("value = 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


def _plan(repo):
    return json.loads(asyncio.run(server.plan_pr_review(str(repo), "HEAD~1")))


def _agent_task_id(plan):
    return next(t["task_id"] for t in plan["tasks"] if t.get("kind") == "agent")


def _submit(plan_id, task_id, findings):
    return json.loads(asyncio.run(server.submit_task_findings(plan_id, task_id, findings)))


def _stored_check(plan_id, task_id) -> CheckResult:
    stored = [t for t in repositories.load_review_tasks(plan_id) if t["task_id"] == task_id]
    return CheckResult(**stored[0]["result"][0])


def test_findings_become_an_agent_review_check_result(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    task_id = _agent_task_id(plan)

    out = _submit(plan["plan_id"], task_id, [
        {"file": "app/a.py", "line": 95, "severity": "high",
         "category": "correctness", "message": "condição invertida", "suggestion": "usar <="},
        {"file": "app/c.py", "line": 3, "severity": "medium", "message": "nome ruim"},
    ])
    check = _stored_check(plan["plan_id"], task_id)

    assert out["accepted"] == 2
    assert out["by_severity"] == {"high": 1, "medium": 1, "low": 0}
    assert check.check == "agent_review"
    assert check.status == GateStatus.failed
    assert check.violations[0].category == "correctness"
    assert check.violations[0].suggestion == "usar <="
    assert check.metrics == {
        "high_issues": 1, "medium_issues": 1, "low_issues": 0, "findings_count": 2,
    }


def test_block_on_none_caps_severity_at_warning(tmp_path, db):
    plan = _plan(_repo(tmp_path, block_on="none"))
    task_id = _agent_task_id(plan)

    _submit(plan["plan_id"], task_id, [
        {"file": "app/a.py", "severity": "high", "message": "problema grave"},
    ])

    assert _stored_check(plan["plan_id"], task_id).status == GateStatus.warning


def test_empty_findings_is_a_valid_pass(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    task_id = _agent_task_id(plan)

    out = _submit(plan["plan_id"], task_id, [])

    assert out["accepted"] == 0
    assert _stored_check(plan["plan_id"], task_id).status == GateStatus.passed


def test_invalid_severity_is_rejected_with_a_clear_error(tmp_path, db):
    plan = _plan(_repo(tmp_path))

    out = _submit(plan["plan_id"], _agent_task_id(plan), [
        {"file": "app/a.py", "severity": "critical", "message": "…"},
    ])

    assert "severity" in out["error"]


def test_finding_without_file_or_message_is_rejected(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    task_id = _agent_task_id(plan)

    assert "error" in _submit(plan["plan_id"], task_id, [{"severity": "low", "message": "x"}])
    assert "error" in _submit(plan["plan_id"], task_id, [{"file": "a.py", "severity": "low"}])


def test_resubmitting_overwrites_instead_of_duplicating(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    task_id = _agent_task_id(plan)

    _submit(plan["plan_id"], task_id, [{"file": "app/a.py", "severity": "low", "message": "a"}])
    _submit(plan["plan_id"], task_id, [{"file": "app/a.py", "severity": "high", "message": "b"}])

    stored = [t for t in repositories.load_review_tasks(plan["plan_id"]) if t["task_id"] == task_id]
    assert len(stored) == 1
    assert _stored_check(plan["plan_id"], task_id).violations[0].message == "b"


def test_submit_rejects_a_deterministic_task(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    deterministic = next(t["task_id"] for t in plan["tasks"] if t.get("kind") == "deterministic")

    out = _submit(plan["plan_id"], deterministic, [])

    assert "error" in out
    assert "run_review_task" in out["error"]

def _all_deterministic(plan_id, plan):
    for task in plan["tasks"]:
        if task.get("kind", "deterministic") == "deterministic":
            asyncio.run(server.run_review_task(plan_id, task["task_id"]))


def test_without_force_pending_agent_tasks_still_block(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    _all_deterministic(plan["plan_id"], plan)

    out = json.loads(asyncio.run(server.get_review_plan(plan["plan_id"])))

    assert out["status"] == "pending"
    assert out["pending_tasks"] == [_agent_task_id(plan)]


def test_force_consolidates_with_missing_agent_tasks(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    _all_deterministic(plan["plan_id"], plan)

    table = asyncio.run(server.get_review_plan(plan["plan_id"], force=True))

    assert "agent_review" in table
    assert "skipped" in table
    assert "sem retorno" in table


def test_force_never_yields_a_clean_pass(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    _all_deterministic(plan["plan_id"], plan)

    asyncio.run(server.get_review_plan(plan["plan_id"], force=True))
    run = repositories.get_gate_run(plan["plan_id"])

    assert run["status"] in ("warning", "failed", "error")


def test_completed_agent_findings_reach_the_consolidated_table(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    _all_deterministic(plan["plan_id"], plan)
    _submit(plan["plan_id"], _agent_task_id(plan), [
        {"file": "app/a.py", "line": 2, "severity": "high",
         "category": "correctness", "message": "bug real", "suggestion": "corrigir"},
    ])

    table = asyncio.run(server.get_review_plan(plan["plan_id"]))

    assert "agent_review" in table
    assert "bug real" in table


def test_agent_findings_do_not_land_in_the_baseline(tmp_path, db):
    repo = _repo(tmp_path)
    plan = _plan(repo)
    record = repositories.load_review_plan(plan["plan_id"])
    payload = record["payload"]
    payload["runtime"]["use_ratchet"] = True
    repositories.save_review_plan(
        plan_id=plan["plan_id"], workspace=record["workspace"],
        repository=record["repository"], branch=record["branch"],
        status="pending", payload=payload,
    )
    _all_deterministic(plan["plan_id"], plan)
    _submit(plan["plan_id"], _agent_task_id(plan), [
        {"file": "app/a.py", "severity": "low", "message": "detalhe"},
    ])

    asyncio.run(server.get_review_plan(plan["plan_id"]))
    baseline = repositories.load_baseline(record["repository"], record["branch"]) or {}

    assert not any(key.startswith("agent_review.") for key in baseline)


def test_forced_consolidation_is_not_cached_as_complete(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    task_id = _agent_task_id(plan)
    _all_deterministic(plan["plan_id"], plan)

    first = asyncio.run(server.get_review_plan(plan["plan_id"], force=True))
    assert "sem retorno" in first
    assert repositories.load_review_plan(plan["plan_id"])["status"] == "pending"

    _submit(plan["plan_id"], task_id, [
        {"file": "app/a.py", "severity": "medium", "message": "achado atrasado"},
    ])
    second = asyncio.run(server.get_review_plan(plan["plan_id"]))

    assert "achado atrasado" in second
    assert "sem retorno" not in second


def test_submit_rejects_a_file_outside_the_task_slice(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    task_id = _agent_task_id(plan)

    out = _submit(plan["plan_id"], task_id, [
        {"file": "app/inexistente.py", "severity": "high", "message": "alucinação"},
    ])

    assert "error" in out
    assert "app/inexistente.py" in out["error"]


def test_submit_is_refused_after_the_plan_is_consolidated(tmp_path, db):
    plan = _plan(_repo(tmp_path))
    task_id = _agent_task_id(plan)
    _all_deterministic(plan["plan_id"], plan)
    _submit(plan["plan_id"], task_id, [])
    asyncio.run(server.get_review_plan(plan["plan_id"]))

    out = _submit(plan["plan_id"], task_id, [
        {"file": "app/a.py", "severity": "high", "message": "tarde demais"},
    ])

    assert "error" in out
    assert "consolidado" in out["error"]
