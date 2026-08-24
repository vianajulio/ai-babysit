import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mcp_server_standalone as server
from app.mcp import gate_tools, review_tools, runtime
from app.storage import database, repositories


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """SQLite próprio por teste, sem tocar em babysit.db."""
    from sqlalchemy import create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(runtime, "_db_ready", False)
    database.init_db()
    return engine


def _call(workspace, files):
    return json.loads(asyncio.run(server.run_local_gate(str(workspace), files)))


def test_run_local_gate_aborts_when_workspace_is_not_a_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_tools, "ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    missing_ws = tmp_path / "nope"

    result = _call(missing_ws, ["Foo.py"])

    assert "error" in result
    assert str(missing_ws) in result["error"]


def test_run_local_gate_aborts_when_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_tools, "ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))

    result = _call(tmp_path, [])

    assert "error" in result


def test_run_local_gate_aborts_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_tools, "ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    (tmp_path / "Foo.py").write_text("x = 1\n")

    result = _call(tmp_path, ["Foo.py", "Missing.py"])

    assert "error" in result
    assert "Missing.py" in result["error"]
    assert "Foo.py" not in result["error"]


def test_run_local_gate_resolves_files_relative_to_workspace(tmp_path, monkeypatch):
    """Leading slash is stripped and joined to workspace, mirroring the runners."""
    monkeypatch.setattr(gate_tools, "ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    (tmp_path / "Foo.py").write_text("x = 1\n")

    result = _call(tmp_path, ["/Foo.py", "/Bar.py"])

    assert "error" in result
    assert "Bar.py" in result["error"]
    assert "Foo.py" not in result["error"]


def test_run_local_gate_passes_validation_when_files_exist(tmp_path, monkeypatch):
    (tmp_path / "Foo.py").write_text("x = 1\n")
    monkeypatch.setattr(gate_tools, "ensure_db", lambda: None)

    captured = {}

    async def fake_gate(**kwargs):
        captured.update(kwargs)
        return {"status": "passed", "checks": []}

    monkeypatch.setattr(gate_tools, "run_local_quality_gate", fake_gate)
    monkeypatch.setattr(gate_tools, "build_quality_gate_table", lambda result, before: "OK")

    out = asyncio.run(server.run_local_gate(str(tmp_path), ["Foo.py"]))

    assert out == "OK"
    assert captured["changed_files"] == ["Foo.py"]


def test_standalone_import_does_not_load_remote_or_http_runtime():
    code = """
import sys
import mcp_server_standalone
assert 'app.providers.azure_devops' not in sys.modules
assert 'app.ai.ollama' not in sys.modules
assert 'uvicorn' not in sys.modules
assert not hasattr(mcp_server_standalone, 'run_azure_pr_gate')
assert not hasattr(mcp_server_standalone, 'list_azure_prs')
"""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("AZURE_"):
            env.pop(key)
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        env=env,
        cwd=Path(__file__).parents[1],
        stdin=subprocess.DEVNULL,
    )


def test_run_local_gate_rejects_existing_file_outside_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(gate_tools, "ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))

    result = _call(workspace, [str(outside)])

    assert "error" in result
    assert "fora do workspace" in result["error"]


def test_run_commit_gate_uses_detached_worktree_and_cleans_it(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (repo / "changed.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "deleted.py").write_text("gone\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    (repo / "changed.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "deleted.py").unlink()
    (repo / "added.py").write_text("new = True\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "change")
    sha = git("rev-parse", "HEAD")

    captured = {}

    async def fake_gate(**kwargs):
        captured.update(kwargs)
        assert kwargs["workspace"].is_dir()
        assert set(kwargs["changed_files"]) == {"changed.py", "added.py"}
        assert not (kwargs["workspace"] / "deleted.py").exists()
        return {"status": "passed", "checks": []}

    monkeypatch.setattr(gate_tools, "ensure_db", lambda: None)
    monkeypatch.setattr(gate_tools, "run_local_quality_gate", fake_gate)
    monkeypatch.setattr(gate_tools, "build_quality_gate_table", lambda result, before: "OK")

    original_head = git("rev-parse", "HEAD")
    output = asyncio.run(server.run_commit_gate(str(repo), sha))

    assert output == "OK"
    assert captured["use_ratchet"] is False
    assert captured["workspace"] != repo.resolve()
    assert not captured["workspace"].exists()
    assert git("rev-parse", "HEAD") == original_head
    assert len(git("worktree", "list").splitlines()) == 1


def test_review_mode_reaches_the_prompt():
    from app.ai.prompts import build_review_prompt

    strict = build_review_prompt(
        standards="", language="python", file_path="a.py", code="x = 1",
        diff=None, review_mode="strict",
    )
    suggest = build_review_prompt(
        standards="", language="python", file_path="a.py", code="x = 1",
        diff=None, review_mode="suggest_only",
    )

    assert strict != suggest
    assert "suggest_only" in suggest or "sugestão" in suggest.lower()


def test_unknown_review_mode_is_rejected():
    import pytest

    from app.ai.prompts import build_review_prompt

    with pytest.raises(ValueError, match="review_mode"):
        build_review_prompt(
            standards="", language="python", file_path="a.py", code="x = 1",
            diff=None, review_mode="turbo",
        )


def test_commit_gate_refuses_ratchet_without_an_explicit_branch(tmp_path, db, monkeypatch):
    monkeypatch.setattr(gate_tools, "ensure_db", lambda: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("v = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True, capture_output=True)

    out = json.loads(asyncio.run(
        server.run_commit_gate(str(repo), "HEAD", use_ratchet=True)
    ))

    assert "branch" in out["error"]


def test_gate_run_summary_counts_errored_checks(db):
    from app.gates.models import CheckResult, GateRun, GateStatus

    run = GateRun(
        run_id="run-err", pr_id=0, status=GateStatus.error,
        checks=[
            CheckResult(check="complexity", status=GateStatus.error, metrics={"error": "timeout"}),
            CheckResult(check="file_size", status=GateStatus.passed),
        ],
    )
    repositories.save_gate_run(run)

    summary = json.loads(asyncio.run(server.get_gate_run_summary("run-err")))

    assert summary["failed_checks"] == 1
    assert summary["failed"] == ["complexity"]
