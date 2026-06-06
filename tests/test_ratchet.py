from app.gates import ratchet
from app.gates.models import CheckResult, GateStatus


def test_ratchet_marks_big_o_check_failed_when_metric_worsens():
    checks = [
        CheckResult(
            check="big_o",
            status=GateStatus.passed,
            metrics={"high_issues": 2, "violations_count": 2},
        )
    ]

    updated = ratchet.apply(checks, {"big_o.high_issues": 1, "big_o.violations_count": 1})

    assert updated[0].status == GateStatus.failed


def test_ratchet_compares_big_o_high_issues_directly():
    checks = [
        CheckResult(
            check="big_o",
            status=GateStatus.passed,
            metrics={"high_issues": 2},
        )
    ]

    updated = ratchet.apply(checks, {"big_o.high_issues": 1})

    assert updated[0].status == GateStatus.failed


def test_ratchet_ignores_big_o_low_issues_and_files_analyzed():
    checks = [
        CheckResult(
            check="big_o",
            status=GateStatus.passed,
            metrics={"low_issues": 4, "files_analyzed": 10},
        )
    ]

    updated = ratchet.apply(checks, {"big_o.low_issues": 1, "big_o.files_analyzed": 1})

    assert updated[0].status == GateStatus.passed
