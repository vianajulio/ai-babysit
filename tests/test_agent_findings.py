from app.gates.models import Violation


def test_violation_accepts_category_and_suggestion():
    violation = Violation(
        file="app/x.py",
        line=10,
        severity="high",
        category="correctness",
        message="condição invertida",
        suggestion="trocar < por <=",
    )

    assert violation.category == "correctness"
    assert violation.suggestion == "trocar < por <="


def test_violation_defaults_keep_existing_runners_working():
    violation = Violation(file="app/x.py", line=1, message="arquivo grande")

    assert violation.severity == "medium"
    assert violation.category == ""
    assert violation.suggestion == ""

from app.gates.models import CheckResult, GateStatus
from app.gates.review_plan import findings_to_check_result


def test_findings_to_check_result_is_pure_and_maps_severity():
    low = findings_to_check_result([{"file": "a.py", "severity": "low", "message": "x"}], "high")

    assert low.status == GateStatus.passed
    assert low.violations[0].line is None
    assert low.metrics["low_issues"] == 1


def test_block_on_is_normalized_and_validated():
    high = [{"file": "a.py", "severity": "high", "message": "x"}]

    assert findings_to_check_result(high, "High").status == GateStatus.failed
    assert findings_to_check_result(high, "").status == GateStatus.failed
    assert findings_to_check_result(high, None).status == GateStatus.failed
    assert findings_to_check_result(high, "none").status == GateStatus.warning
    assert findings_to_check_result(high, " NONE ").status == GateStatus.warning

    import pytest

    with pytest.raises(ValueError, match="block_on"):
        findings_to_check_result(high, "medium")


