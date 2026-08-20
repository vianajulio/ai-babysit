import asyncio

import pytest

from app.gates import review_plan
from app.gates.models import CheckResult, GateStatus, Violation
from app.gates.review_plan import aggregate_checks as aggregate
from app.storage import repositories


def _fs(
    files: list[str] | None = None,
    *,
    max_file_lines: int | None = None,
    violations_count: int | None = None,
    status: str = "passed",
    error: str | None = None,
) -> list[CheckResult]:
    """Um resultado parcial de `file_size` (uma "parte" pronta para `aggregate`)."""
    metrics: dict = {}
    if max_file_lines is not None:
        metrics["max_file_lines"] = max_file_lines
    if violations_count is not None:
        metrics["violations_count"] = violations_count
    if error is not None:
        metrics["error"] = error

    violations = [Violation(file=path) for path in (files or [])]
    return [
        CheckResult(
            check="file_size",
            status=GateStatus(status),
            metrics=metrics,
            violations=violations,
        )
    ]


def _dup(percent: float) -> list[CheckResult]:
    """Um resultado parcial de `duplication` (só a task `global` produz esse check)."""
    return [
        CheckResult(
            check="duplication",
            status=GateStatus.passed,
            metrics={"duplication_percent": percent},
        )
    ]


def test_aggregate_unions_violations_of_the_same_check():
    checks = aggregate([_fs(["a.py"]), _fs(["b.py"])])
    assert [v.file for v in checks[0].violations] == ["a.py", "b.py"]


def test_aggregate_sums_counters_but_keeps_max_for_max_metrics():
    checks = aggregate([
        _fs(max_file_lines=420, violations_count=1),
        _fs(max_file_lines=900, violations_count=2),
    ])
    assert checks[0].metrics["max_file_lines"] == 900
    assert checks[0].metrics["violations_count"] == 3


def test_aggregate_keeps_global_metric_untouched():
    # duplication_percent vem só da task global; nunca soma
    assert aggregate([_dup(7.5)])[0].metrics["duplication_percent"] == 7.5


def test_worst_status_wins():
    checks = aggregate([_fs(status="passed"), _fs(status="failed")])
    assert checks[0].status is GateStatus.failed


def test_error_in_one_shard_is_surfaced_not_swallowed():
    checks = aggregate([_fs(status="passed"), _fs(status="error", error="lizard timeout")])
    assert checks[0].status is GateStatus.error
    assert "lizard timeout" in str(checks[0].metrics)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isola cada teste em um SQLite próprio, sem tocar em babysit.db."""
    from sqlalchemy import create_engine

    from app.storage import database

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    return engine


def test_finalize_plan_persists_gate_run_and_returns_the_usual_table(db):
    table = asyncio.run(
        review_plan.finalize_plan(
            {"plan_id": "plan-1"},
            [_fs(["a.py"], status="passed")],
            config={"quality_gate": {"checks": {}}},
            repository="repo",
            branch="main",
        )
    )

    assert "file_size" in table
    assert "passed" in table
    assert repositories.load_baseline("repo", "main") is None  # use_ratchet=False por padrão


def test_finalize_plan_saves_baseline_only_when_ratchet_passes(db):
    asyncio.run(
        review_plan.finalize_plan(
            {"plan_id": "plan-2"},
            [_fs(max_file_lines=50, status="passed")],
            config={"quality_gate": {"checks": {}}},
            repository="repo",
            branch="main",
            use_ratchet=True,
        )
    )

    assert repositories.load_baseline("repo", "main") == {"file_size.max_file_lines": 50}


def test_finalize_plan_does_not_save_baseline_when_status_is_not_passed(db):
    asyncio.run(
        review_plan.finalize_plan(
            {"plan_id": "plan-3"},
            [_fs(status="error", error="lizard timeout")],
            config={"quality_gate": {"checks": {}}},
            repository="repo",
            branch="main",
            use_ratchet=True,
        )
    )

    assert repositories.load_baseline("repo", "main") is None


def test_finalize_plan_applies_ratchet_once_over_the_aggregated_result(db):
    repositories.save_baseline("repo", "main", {"file_size.max_file_lines": 100})

    table = asyncio.run(
        review_plan.finalize_plan(
            {"plan_id": "plan-4"},
            [_fs(max_file_lines=60, status="passed"), _fs(max_file_lines=90, status="passed")],
            config={"quality_gate": {"checks": {}}},
            repository="repo",
            branch="main",
            use_ratchet=True,
        )
    )

    # max(60, 90) = 90 < 100 do baseline: não regrediu, continua passed.
    assert "passed" in table
    assert repositories.load_baseline("repo", "main")["file_size.max_file_lines"] == 90


def _agent(findings: list[tuple[str, int, str]], severity: str = "high") -> list[CheckResult]:
    """Um resultado parcial de `agent_review` (uma task de subagente)."""
    counts = {"high": 0, "medium": 0, "low": 0}
    violations = []
    for file_path, line, message in findings:
        counts[severity] += 1
        violations.append(
            Violation(file=file_path, line=line, severity=severity, message=message)
        )
    return [
        CheckResult(
            check="agent_review",
            status=GateStatus.failed if severity == "high" else GateStatus.warning,
            metrics={
                "high_issues": counts["high"],
                "medium_issues": counts["medium"],
                "low_issues": counts["low"],
                "findings_count": len(violations),
            },
            violations=violations,
        )
    ]


def test_agent_metrics_never_reach_the_baseline():
    checks = [
        CheckResult(check="file_size", status=GateStatus.passed, metrics={"max_file_lines": 120}),
        CheckResult(
            check="agent_review",
            status=GateStatus.failed,
            metrics={"high_issues": 2, "findings_count": 5},
        ),
    ]

    assert review_plan.baseline_metrics(checks) == {"file_size.max_file_lines": 120}


def test_agent_findings_are_merged_and_deduped_across_tasks():
    parts = [
        _agent([("a.py", 10, "X")]),
        _agent([("a.py", 10, "X"), ("b.py", 3, "Y")]),
    ]

    merged = aggregate(parts)

    assert len(merged) == 1
    assert merged[0].check == "agent_review"
    assert len(merged[0].violations) == 2
    assert merged[0].metrics["findings_count"] == 3
    assert merged[0].metrics["high_issues"] == 3
