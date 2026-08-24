import asyncio

from app.gates import ratchet
from app.gates.models import CheckResult, GateStatus, Violation
from app.runners.file_size import FileSizeRunner


def test_file_size_reports_line_metrics(tmp_path):
    workspace = tmp_path
    rel_path = "ContaRepository.cs"
    # Linhas de código de verdade: no modo `code` (padrão) comentário não conta.
    (workspace / rel_path).write_text("\n".join(["var x = 1;"] * 276), encoding="utf-8")

    result = asyncio.run(FileSizeRunner(max_lines_per_file=80).run(workspace, [rel_path]))

    assert result.status == GateStatus.failed
    assert result.metrics["max_file_lines"] == 276
    assert result.metrics["violations_count"] == 1


def test_file_size_does_not_count_csharp_primary_constructor_class_as_function(tmp_path):
    workspace = tmp_path
    rel_path = "ContaRepository.cs"
    body = "\n".join(["    public Task<int> GetAsync() => Task.FromResult(1);"] * 20)
    (workspace / rel_path).write_text(
        f"""using System.Threading.Tasks;

namespace App;

public class ContaRepository(DbContext context)
{{
{body}
}}
""",
        encoding="utf-8",
    )

    result = asyncio.run(FileSizeRunner(max_lines_per_file=400, max_lines_per_function=80).run(workspace, [rel_path]))

    assert result.status == GateStatus.passed
    assert result.metrics["max_file_lines"] == 25      # 27 brutas, 2 em branco
    assert result.metrics["max_function_lines"] == 0
    assert result.violations == []


def test_ratchet_downgrades_existing_file_size_violation_when_not_worse():
    check = CheckResult(
        check="file_size",
        status=GateStatus.failed,
        metrics={"violations_count": 1, "max_file_lines": 276, "max_function_lines": 0},
        violations=[
            Violation(
                file="ContaRepository.cs",
                message="Arquivo com 276 linhas (limite: 80)",
                current_value=276,
                allowed_value=80,
            )
        ],
    )

    updated = ratchet.apply(
        [check],
        {"file_size.violations_count": 1, "file_size.max_file_lines": 276, "file_size.max_function_lines": 0},
    )

    assert updated[0].status == GateStatus.passed


def test_ratchet_keeps_file_size_failed_when_line_count_worsens():
    check = CheckResult(
        check="file_size",
        status=GateStatus.failed,
        metrics={"violations_count": 1, "max_file_lines": 276, "max_function_lines": 0},
    )

    updated = ratchet.apply(
        [check],
        {"file_size.violations_count": 1, "file_size.max_file_lines": 213, "file_size.max_function_lines": 0},
    )

    assert updated[0].status == GateStatus.failed
