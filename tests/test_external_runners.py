import asyncio
import json
from pathlib import Path

import httpx

from app.gates.models import GateStatus
from app.runners.big_o import BigORunner
from app.runners.complexity import ComplexityRunner
from app.runners.duplication import DuplicationRunner
from app.runners.file_size import FileSizeRunner
from app.runners.secrets import SecretsRunner


def test_complexity_runner_skips_when_lizard_is_missing(tmp_path, monkeypatch):
    async def missing_command(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("app.runners.complexity.asyncio.create_subprocess_exec", missing_command)

    result = asyncio.run(ComplexityRunner().run(tmp_path, []))

    assert result.status == GateStatus.skipped
    assert result.metrics == {"error": "lizard não instalado"}


def test_duplication_runner_skips_when_jscpd_is_missing(tmp_path, monkeypatch):
    async def missing_command(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", missing_command)

    result = asyncio.run(DuplicationRunner().run(tmp_path, []))

    assert result.status == GateStatus.skipped
    assert result.metrics == {"error": "jscpd não instalado"}


def test_duplication_runner_passes_global_duplication_when_no_changed_file_violation(tmp_path, monkeypatch):
    changed = tmp_path / "Changed.cs"
    unchanged = tmp_path / "Unchanged.cs"
    changed.write_text("public class Changed {}", encoding="utf-8")
    unchanged.write_text("public class Unchanged {}", encoding="utf-8")
    report_dir = tmp_path.parent / "reports-a"
    _write_jscpd_report(
        report_dir,
        percentage=27.5,
        duplicates=[_clone(str(unchanged), str(unchanged))],
    )

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", _successful_process)

    result = asyncio.run(
        DuplicationRunner(max_percent=5, fail_only_on_changed_files=True, report_dir=report_dir)
        .run(tmp_path, ["Changed.cs"])
    )

    assert result.status == GateStatus.passed
    assert result.metrics["duplication_percent"] == 27.5
    assert result.violations == []


def test_duplication_runner_fails_when_changed_file_has_duplication(tmp_path, monkeypatch):
    changed = tmp_path / "Changed.cs"
    other = tmp_path / "Other.cs"
    changed.write_text("public class Changed {}", encoding="utf-8")
    other.write_text("public class Other {}", encoding="utf-8")
    report_dir = tmp_path.parent / "reports-b"
    _write_jscpd_report(
        report_dir,
        percentage=27.5,
        duplicates=[_clone(str(changed), str(other))],
    )

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", _successful_process)

    result = asyncio.run(
        DuplicationRunner(max_percent=5, fail_only_on_changed_files=True, report_dir=report_dir)
        .run(tmp_path, ["Changed.cs"])
    )

    assert result.status == GateStatus.failed
    assert result.violations[0].file == "Changed.cs"


def test_duplication_runner_isolates_report_dir_when_run_concurrently(tmp_path, monkeypatch):
    changed = tmp_path / "Changed.cs"
    changed.write_text("public class Changed {}", encoding="utf-8")

    call_log: list[Path] = []

    async def fake_exec(*cmd, **kwargs):
        idx = cmd.index("--output")
        output_dir = Path(cmd[idx + 1])
        call_index = len(call_log)
        call_log.append(output_dir)
        percentage = 10.0 if call_index == 0 else 20.0
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "jscpd-report.json").write_text(
            json.dumps({"statistics": {"total": {"percentage": percentage}}, "duplicates": []}),
            encoding="utf-8",
        )
        await asyncio.sleep(0.05 if call_index == 0 else 0.01)
        return _Process()

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", fake_exec)

    async def run_both():
        runner_a = DuplicationRunner()
        runner_b = DuplicationRunner()
        task_a = asyncio.create_task(runner_a.run(tmp_path, ["Changed.cs"]))
        task_b = asyncio.create_task(runner_b.run(tmp_path, ["Changed.cs"]))
        return await asyncio.gather(task_a, task_b)

    result_a, result_b = asyncio.run(run_both())

    assert len(call_log) == 2
    assert call_log[0] != call_log[1]
    assert result_a.metrics["duplication_percent"] == 10.0
    assert result_b.metrics["duplication_percent"] == 20.0


def test_secrets_runner_skips_when_gitleaks_is_missing(tmp_path, monkeypatch):
    async def missing_command(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("app.runners.secrets.asyncio.create_subprocess_exec", missing_command)

    result = asyncio.run(SecretsRunner().run(tmp_path, []))

    assert result.status == GateStatus.skipped
    assert result.metrics == {"error": "gitleaks não instalado"}


def test_secrets_runner_isolates_report_dir_when_run_concurrently(tmp_path, monkeypatch):
    changed = tmp_path / "Changed.cs"
    changed.write_text("public class Changed {}", encoding="utf-8")

    call_log: list[Path] = []

    async def fake_exec(*cmd, **kwargs):
        idx = cmd.index("--report-path")
        report_path = Path(cmd[idx + 1])
        call_index = len(call_log)
        call_log.append(report_path)
        findings = [] if call_index == 0 else [
            {"File": str(changed), "StartLine": 1, "RuleID": "aws-key"}
        ]
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(findings), encoding="utf-8")
        await asyncio.sleep(0.05 if call_index == 0 else 0.01)
        return _Process()

    monkeypatch.setattr("app.runners.secrets.asyncio.create_subprocess_exec", fake_exec)

    async def run_both():
        runner_a = SecretsRunner()
        runner_b = SecretsRunner()
        task_a = asyncio.create_task(runner_a.run(tmp_path, ["Changed.cs"]))
        task_b = asyncio.create_task(runner_b.run(tmp_path, ["Changed.cs"]))
        return await asyncio.gather(task_a, task_b)

    result_a, result_b = asyncio.run(run_both())

    assert len(call_log) == 2
    assert call_log[0] != call_log[1]
    assert result_a.metrics["secrets_found"] == 0
    assert result_b.metrics["secrets_found"] == 1


def test_file_size_runner_ignores_generated_designer_and_html_templates(tmp_path):
    generated = tmp_path / "Migration.Designer.cs"
    template = tmp_path / "template.html"
    generated.write_text("public void Build() {\n" + "x();\n" * 500 + "}\n", encoding="utf-8")
    template.write_text("<div></div>\n" * 500, encoding="utf-8")

    result = asyncio.run(FileSizeRunner().run(tmp_path, [generated.name, template.name]))

    assert result.status == GateStatus.passed
    assert result.metrics["violations_count"] == 0
    assert result.metrics["max_file_lines"] == 0
    assert result.metrics["max_function_lines"] == 0


def test_file_size_runner_ignores_configured_migrations_directory(tmp_path):
    migration = tmp_path / "src" / "Persistence" / "Migrations" / "v082.cs"
    migration.parent.mkdir(parents=True)
    migration.write_text("public void Build() {\n" + "x();\n" * 500 + "}\n", encoding="utf-8")

    result = asyncio.run(
        FileSizeRunner(exclude=["*Migrations*"]).run(
            tmp_path,
            ["src/Persistence/Migrations/v082.cs"],
        )
    )

    assert result.status == GateStatus.passed
    assert result.metrics["violations_count"] == 0
    assert result.metrics["max_file_lines"] == 0
    assert result.metrics["max_function_lines"] == 0


def test_big_o_runner_skips_when_no_changed_csharp_files(tmp_path):
    (tmp_path / "Foo.py").write_text("print('ok')", encoding="utf-8")

    result = asyncio.run(BigORunner().run(tmp_path, ["Foo.py"]))

    assert result.status == GateStatus.skipped
    assert result.metrics["files_considered"] == 0


def test_big_o_runner_skips_when_no_candidate_files(tmp_path):
    (tmp_path / "Foo.cs").write_text("public class Foo {}", encoding="utf-8")

    result = asyncio.run(BigORunner().run(tmp_path, ["Foo.cs"]))

    assert result.status == GateStatus.skipped
    assert result.metrics["files_considered"] == 1
    assert result.metrics["files_analyzed"] == 0


def test_big_o_runner_analyzes_linq_chain_without_loop(tmp_path, monkeypatch):
    (tmp_path / "Foo.cs").write_text(
        "public void Run() { users.Where(u => u.Active).OrderBy(u => u.Name).ToList(); }",
        encoding="utf-8",
    )

    async def generate(_prompt, expect_json=False):
        assert expect_json is True
        return json.dumps({"summary": "ok", "issues": []})

    monkeypatch.setattr("app.runners.big_o.ollama.generate", generate)

    result = asyncio.run(BigORunner().run(tmp_path, ["Foo.cs"]))

    assert result.status == GateStatus.passed
    assert result.metrics["files_analyzed"] == 1


def test_big_o_runner_fails_on_high_issue(tmp_path, monkeypatch):
    (tmp_path / "Foo.cs").write_text(
        "public void Run() { foreach (var item in items) { users.Any(u => u.Id == item.Id); } }",
        encoding="utf-8",
    )

    async def generate(_prompt, expect_json=False):
        assert expect_json is True
        return json.dumps({
            "summary": "quadratic scan",
            "issues": [{
                "severity": "high",
                "line": 1,
                "current_complexity": "O(n^2)",
                "suggested_complexity": "O(n)",
                "problem": "Any inside loop",
                "suggestion": "Use HashSet",
            }],
        })

    monkeypatch.setattr("app.runners.big_o.ollama.generate", generate)

    result = asyncio.run(BigORunner().run(tmp_path, ["Foo.cs"]))

    assert result.status == GateStatus.failed
    assert result.metrics["high_issues"] == 1
    assert result.violations[0].severity == "high"
    assert "O(n^2)" in result.violations[0].message


def test_big_o_runner_warns_on_medium_issue(tmp_path, monkeypatch):
    (tmp_path / "Foo.cs").write_text(
        "public void Run() { foreach (var item in items) { values.Where(v => v.Id == item.Id).ToList(); } }",
        encoding="utf-8",
    )

    async def generate(_prompt, expect_json=False):
        assert expect_json is True
        return json.dumps({
            "summary": "possible repeated filtering",
            "issues": [{
                "severity": "medium",
                "line": 1,
                "current_complexity": "O(n*m)",
                "suggested_complexity": "O(n + m)",
                "problem": "Where inside loop",
                "suggestion": "Pre-group values",
            }],
        })

    monkeypatch.setattr("app.runners.big_o.ollama.generate", generate)

    result = asyncio.run(BigORunner().run(tmp_path, ["Foo.cs"]))

    assert result.status == GateStatus.warning
    assert result.metrics["medium_issues"] == 1


def test_big_o_runner_warns_on_high_when_fail_on_high_is_false(tmp_path, monkeypatch):
    (tmp_path / "Foo.cs").write_text(
        "public async Task Run() { foreach (var item in items) { await repository.GetAsync(item.Id); } }",
        encoding="utf-8",
    )

    async def generate(_prompt, expect_json=False):
        assert expect_json is True
        return json.dumps({
            "summary": "database call inside loop",
            "issues": [{
                "severity": "high",
                "line": 1,
                "current_complexity": "O(n database calls)",
                "suggested_complexity": "O(1 database call)",
                "problem": "Repository call inside loop",
                "suggestion": "Batch query",
            }],
        })

    monkeypatch.setattr("app.runners.big_o.ollama.generate", generate)

    result = asyncio.run(BigORunner(fail_on_high=False).run(tmp_path, ["Foo.cs"]))

    assert result.status == GateStatus.warning
    assert result.metrics["high_issues"] == 1


def test_big_o_runner_errors_on_malformed_json(tmp_path, monkeypatch):
    (tmp_path / "Foo.cs").write_text(
        "public void Run() { foreach (var item in items) { users.Any(u => u.Id == item.Id); } }",
        encoding="utf-8",
    )

    async def generate(_prompt, expect_json=False):
        assert expect_json is True
        return "not json"

    monkeypatch.setattr("app.runners.big_o.ollama.generate", generate)

    result = asyncio.run(BigORunner().run(tmp_path, ["Foo.cs"]))

    assert result.status == GateStatus.error
    assert "error" in result.metrics


def test_big_o_runner_truncates_large_candidate_before_prompt(tmp_path, monkeypatch):
    (tmp_path / "Foo.cs").write_text(
        "public void Run() { foreach (var item in items) { users.Any(u => u.Id == item.Id); } }\n"
        + "// filler\n" * 200,
        encoding="utf-8",
    )
    captured = {}

    def build_prompt(language, file_path, code):
        captured["code"] = code
        return "prompt"

    async def generate(_prompt, expect_json=False):
        return json.dumps({"summary": "ok", "issues": []})

    monkeypatch.setattr("app.runners.big_o.build_big_o_prompt", build_prompt)
    monkeypatch.setattr("app.runners.big_o.ollama.generate", generate)

    result = asyncio.run(BigORunner(max_code_chars=120).run(tmp_path, ["Foo.cs"]))

    assert result.status == GateStatus.passed
    assert len(captured["code"]) <= 120


def test_big_o_runner_reports_ollama_timeout_as_skipped(tmp_path, monkeypatch):
    (tmp_path / "Foo.cs").write_text(
        "public void Run() { foreach (var item in items) { users.Any(u => u.Id == item.Id); } }",
        encoding="utf-8",
    )

    async def generate(_prompt, expect_json=False):
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr("app.runners.big_o.ollama.generate", generate)

    result = asyncio.run(BigORunner().run(tmp_path, ["Foo.cs"]))

    assert result.status == GateStatus.skipped
    assert result.metrics["error"] == "Ollama timeout ao analisar Big O"


class _Process:
    async def communicate(self):
        return b"", b""

    def kill(self):
        pass


async def _successful_process(*args, **kwargs):
    return _Process()


def _write_jscpd_report(report_dir, percentage: float, duplicates: list[dict]):
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "jscpd-report.json").write_text(
        json.dumps({"statistics": {"total": {"percentage": percentage}}, "duplicates": duplicates}),
        encoding="utf-8",
    )


def _clone(source_a: str, source_b: str) -> dict:
    return {
        "duplicationA": [{"sourceId": source_a, "start": {"line": 1}}],
        "duplicationB": [{"sourceId": source_b, "start": {"line": 1}}],
    }
