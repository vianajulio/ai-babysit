import asyncio

from app.gates.models import CheckResult, GateStatus
from app.gates.orchestrator import run_local_quality_gate


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
