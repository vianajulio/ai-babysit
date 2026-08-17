import asyncio

from app.gates import orchestrator
from app.gates.models import CheckResult, GateStatus
from app.gates.orchestrator import run_local_quality_gate
from settings import Settings


def _full_config():
    return {
        "quality_gate": {
            "checks": {
                "file_size": {"enabled": True},
                "complexity": {"enabled": True},
                "duplication": {"enabled": True},
                "secrets": {"enabled": True},
            }
        }
    }


def test_run_local_quality_gate_persists_pr_zero(monkeypatch, tmp_path):
    saved = {}

    async def run_checks(workspace, changed_files):
        assert workspace == tmp_path
        assert changed_files == ["Foo.cs"]
        return [CheckResult(check="file_size", status=GateStatus.passed)]

    async def annotate(checks):
        return checks

    def save_gate_run(gate_run):
        saved["gate_run"] = gate_run

    monkeypatch.setattr("app.gates.orchestrator._run_checks", run_checks)
    monkeypatch.setattr("app.gates.orchestrator._annotate_with_ai", annotate)
    monkeypatch.setattr("app.gates.orchestrator.repositories.save_gate_run", save_gate_run)

    result = asyncio.run(
        run_local_quality_gate(
            tmp_path,
            ["Foo.cs"],
            repository="energia",
            branch="local",
            use_ratchet=False,
        )
    )

    assert result["pr_id"] == 0
    assert result["repository"] == "energia"
    assert result["source_branch"] == "local"
    assert result["target_branch"] == "local"
    assert result["status"] == "passed"
    assert saved["gate_run"].pr_id == 0


def test_local_ai_decision_uses_workspace_override(monkeypatch, tmp_path):
    (tmp_path / ".babysit.yml").write_text(
        "quality_gate:\n  checks:\n    ai_review:\n      enabled: false\n",
        encoding="utf-8",
    )
    captured = {}

    async def run_checks(workspace, changed_files):
        return [CheckResult(check="file_size", status=GateStatus.passed)]

    async def annotate(checks, config):
        captured["enabled"] = config["quality_gate"]["checks"]["ai_review"]["enabled"]
        return checks

    monkeypatch.setattr(
        Settings,
        "quality_gate_config",
        property(lambda self: {"quality_gate": {"checks": {"ai_review": {"enabled": True}}}}),
    )
    monkeypatch.setattr("app.gates.orchestrator._run_checks", run_checks)
    monkeypatch.setattr("app.gates.orchestrator._annotate_with_ai", annotate)
    monkeypatch.setattr("app.gates.orchestrator.repositories.save_gate_run", lambda gate_run: None)

    asyncio.run(run_local_quality_gate(tmp_path, ["Foo.py"]))

    assert captured["enabled"] is False


def test_build_runners_filters_by_requested_checks():
    runners = orchestrator._build_runners(_full_config(), only=["file_size"])
    assert [r.name for r in runners] == ["file_size"]


def test_run_review_task_gate_does_not_touch_baseline(monkeypatch, tmp_path):
    async def run_checks(workspace, changed_files, only=None):
        assert workspace == tmp_path
        assert changed_files == ["Foo.cs"]
        assert only == ["file_size"]
        return [CheckResult(check="file_size", status=GateStatus.passed)]

    def explode(*args, **kwargs):
        raise AssertionError("baseline/ratchet must not be touched by a partial task result")

    monkeypatch.setattr("app.gates.orchestrator._run_checks", run_checks)
    monkeypatch.setattr("app.gates.orchestrator.repositories.save_baseline", explode)
    monkeypatch.setattr("app.gates.orchestrator.repositories.load_baseline", explode)
    monkeypatch.setattr("app.gates.orchestrator.repositories.save_gate_run", explode)
    monkeypatch.setattr("app.gates.orchestrator.ratchet.apply", explode)

    checks = asyncio.run(
        orchestrator.run_review_task_gate(tmp_path, ["Foo.cs"], ["file_size"])
    )

    assert checks == [CheckResult(check="file_size", status=GateStatus.passed)]
