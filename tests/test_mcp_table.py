from app.gates.mcp_table import build_quality_gate_table


def test_build_quality_gate_table_compares_before_and_after_metrics():
    table = build_quality_gate_table(
        {
            "checks": [
                {
                    "check": "file_size",
                    "status": "passed",
                    "metrics": {"max_file_lines": 160, "violations_count": 0},
                },
                {
                    "check": "duplication",
                    "status": "failed",
                    "metrics": {"duplication_percent": 6.1},
                },
            ]
        },
        {
            "file_size.max_file_lines": 213,
            "file_size.violations_count": 0,
            "duplication.duplication_percent": 4.2,
        },
    )

    assert table.splitlines()[0] == "| Check | Status | Métrica | Antes | Depois | Resultado |"
    assert "| file_size | passed | max_file_lines | 213 | 160 | melhorou |" in table
    assert "| file_size | passed | violations_count | 0 | 0 | igual |" in table
    assert "| duplication | failed | duplication_percent | 4.2 | 6.1 | piorou |" in table


def test_build_quality_gate_table_handles_missing_baseline():
    table = build_quality_gate_table(
        {"checks": [{"check": "complexity", "status": "passed", "metrics": {"violations_count": 0}}]},
    )

    assert "| complexity | passed | violations_count | - | 0 | sem baseline |" in table


def test_build_quality_gate_table_returns_error_row_without_checks():
    table = build_quality_gate_table({"error": "falha"})

    assert table.splitlines()[-1] == "| quality_gate | error | error | - | falha | - |"


def _agent_run(count: int, severity: str = "high", *, run_id: str = "run-1") -> dict:
    violations = [
        {
            "file": f"app/gates/a{index}.py",
            "line": 95 + index,
            "severity": severity,
            "category": "correctness",
            "message": f"problema {index}",
            "suggestion": f"corrigir {index}",
        }
        for index in range(count)
    ]
    return {
        "run_id": run_id,
        "status": "failed",
        "checks": [
            {
                "check": "agent_review",
                "status": "failed",
                "metrics": {"findings_count": count},
                "violations": violations,
            }
        ],
    }


def test_table_lists_agent_findings_below_the_table():
    table = build_quality_gate_table(_agent_run(3))

    assert "| agent_review |" in table
    assert "### Findings" in table
    assert "`app/gates/a0.py:95` — high — correctness — problema 0" in table
    assert "Sugestão: corrigir 0" in table


def test_findings_are_capped_at_twenty_with_a_remainder_line():
    table = build_quality_gate_table(_agent_run(35))

    assert table.count("— high — correctness —") == 20
    assert "mais 15 findings" in table
    assert "get_gate_run" in table


def test_findings_are_ordered_by_severity():
    run = _agent_run(1, "low", run_id="run-2")
    run["checks"][0]["violations"].extend(_agent_run(1, "high")["checks"][0]["violations"])
    run["checks"][0]["violations"].extend(_agent_run(1, "medium")["checks"][0]["violations"])

    table = build_quality_gate_table(run)

    assert table.index("— high —") < table.index("— medium —") < table.index("— low —")


def test_skipped_agent_review_says_how_many_tasks_are_missing():
    table = build_quality_gate_table({
        "run_id": "run-3",
        "status": "warning",
        "checks": [{"check": "agent_review", "status": "skipped",
                    "metrics": {"skipped_tasks": 2}, "violations": []}],
    })

    assert "2 task(s) sem retorno" in table


def test_run_without_agent_findings_keeps_the_current_output():
    run = {"checks": [{"check": "file_size", "status": "passed",
                       "metrics": {"max_file_lines": 10}, "violations": []}]}

    table = build_quality_gate_table(run)

    assert "###" not in table
    assert table.splitlines()[-1] == "| file_size | passed | max_file_lines | - | 10 | sem baseline |"
