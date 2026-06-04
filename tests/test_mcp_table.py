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
