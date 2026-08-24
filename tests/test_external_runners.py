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


def test_duplication_runner_warns_when_global_duplication_is_absorbed_by_the_filter(tmp_path, monkeypatch):
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

    # Passar limpo esconderia que 27,5% > 5%: o filtro por arquivo alterado
    # absorveu o estouro, e quem lê a tabela precisa saber disso.
    assert result.status == GateStatus.warning
    assert result.metrics["duplication_percent"] == 27.5
    assert "arquivos alterados" in result.metrics["note"]
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


def test_duplication_runner_passes_clean_when_under_the_threshold(tmp_path, monkeypatch):
    changed = tmp_path / "Changed.cs"
    changed.write_text("public class Changed {}", encoding="utf-8")
    report_dir = tmp_path.parent / "reports-under"
    _write_jscpd_report(report_dir, percentage=1.2, duplicates=[])

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", _successful_process)

    result = asyncio.run(
        DuplicationRunner(max_percent=5, fail_only_on_changed_files=True, report_dir=report_dir)
        .run(tmp_path, ["Changed.cs"])
    )

    assert result.status == GateStatus.passed
    assert "note" not in result.metrics


def _long_file(path, lines=350):
    path.write_text("\n".join(f"linha_{i} = {i}" for i in range(lines)), encoding="utf-8")


def test_file_size_fails_when_a_new_file_is_born_too_long(tmp_path):
    from app.runners.file_size import FileSizeRunner

    _long_file(tmp_path / "novo.py")

    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300, new_files={"novo.py"}).run(tmp_path, ["novo.py"])
    )

    assert result.status == GateStatus.failed
    assert result.violations[0].severity == "high"
    assert result.metrics["new_file_violations"] == 1


def test_file_size_only_warns_for_a_pre_existing_long_file(tmp_path):
    from app.runners.file_size import FileSizeRunner

    _long_file(tmp_path / "legado.py")

    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300, new_files=set()).run(tmp_path, ["legado.py"])
    )

    # O PR encostou num arquivo que já nascia grande: apontar, não reprovar.
    assert result.status == GateStatus.warning
    assert result.violations[0].severity == "medium"
    assert result.metrics["new_file_violations"] == 0


def test_file_size_still_fails_when_origin_is_unknown(tmp_path):
    from app.runners.file_size import FileSizeRunner

    _long_file(tmp_path / "sem_origem.py")

    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300).run(tmp_path, ["sem_origem.py"])
    )

    # Sem informação de origem o gate não afrouxa: mantém o rigor de antes.
    assert result.status == GateStatus.failed


def test_file_size_can_be_configured_to_fail_on_pre_existing_files(tmp_path):
    from app.runners.file_size import FileSizeRunner

    _long_file(tmp_path / "legado.py")

    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300, new_files=set(), fail_on_existing_files=True)
        .run(tmp_path, ["legado.py"])
    )

    assert result.status == GateStatus.failed


def test_file_size_fails_when_any_violation_is_in_a_new_file(tmp_path):
    from app.runners.file_size import FileSizeRunner

    _long_file(tmp_path / "legado.py")
    _long_file(tmp_path / "novo.py")

    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300, new_files={"novo.py"})
        .run(tmp_path, ["legado.py", "novo.py"])
    )

    assert result.status == GateStatus.failed
    assert result.metrics["violations_count"] == 2
    assert result.metrics["new_file_violations"] == 1


def test_file_size_warns_between_the_target_and_the_hard_limit(tmp_path):
    from app.runners.file_size import FileSizeRunner

    _long_file(tmp_path / "novo.py", lines=260)

    result = asyncio.run(
        FileSizeRunner(warn_lines_per_file=200, max_lines_per_file=350, new_files={"novo.py"})
        .run(tmp_path, ["novo.py"])
    )

    # Passou do alvo mas não do teto: avisa mesmo sendo arquivo novo.
    assert result.status == GateStatus.warning
    assert result.violations[0].severity == "low"
    assert "alvo" in result.violations[0].message


def test_file_size_counts_code_lines_by_default(tmp_path):
    from app.runners.file_size import FileSizeRunner

    body = "\n".join(f"# comentário {i}" for i in range(400))
    (tmp_path / "doc.py").write_text(body + "\nx = 1\n", encoding="utf-8")

    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300, new_files={"doc.py"}).run(tmp_path, ["doc.py"])
    )

    # 401 linhas brutas, 1 de código: comentar não pode reprovar o arquivo.
    assert result.status == GateStatus.passed
    assert result.metrics["max_file_lines"] == 1


def test_file_size_raw_mode_restores_the_old_counting(tmp_path):
    from app.runners.file_size import FileSizeRunner

    body = "\n".join(f"# comentário {i}" for i in range(400))
    (tmp_path / "doc.py").write_text(body, encoding="utf-8")

    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300, count_mode="raw", new_files={"doc.py"})
        .run(tmp_path, ["doc.py"])
    )

    assert result.status == GateStatus.failed


def test_rust_uses_its_own_limits_and_ignores_inline_tests(tmp_path):
    from app.runners.file_size import FileSizeRunner

    production = "\n".join(f"let x{i} = {i};" for i in range(320))
    tests = "#[cfg(test)]\nmod tests {\n" + "\n".join(
        f"    assert_eq!({i}, {i});" for i in range(200)
    ) + "\n}\n"
    (tmp_path / "lib.rs").write_text(production + "\n" + tests, encoding="utf-8")

    languages = {"rust": {"max_lines_per_file": 500, "exclude_test_blocks": True}}
    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300, languages=languages, new_files={"lib.rs"})
        .run(tmp_path, ["lib.rs"])
    )

    # 520 linhas de código no total, 320 fora dos testes: abaixo do teto do Rust.
    assert result.status == GateStatus.passed
    assert result.metrics["max_file_lines"] == 320


def test_language_override_does_not_leak_to_other_languages(tmp_path):
    from app.runners.file_size import FileSizeRunner

    _long_file(tmp_path / "grande.py", lines=320)
    languages = {"rust": {"max_lines_per_file": 500}}

    result = asyncio.run(
        FileSizeRunner(max_lines_per_file=300, languages=languages, new_files={"grande.py"})
        .run(tmp_path, ["grande.py"])
    )

    assert result.status == GateStatus.failed


def test_duplication_ignore_patterns_reach_the_command(tmp_path, monkeypatch):
    captured = {}

    async def _capture(*cmd, **kwargs):
        captured["cmd"] = cmd
        return await _successful_process(*cmd, **kwargs)

    report_dir = tmp_path.parent / "reports-ignore"
    _write_jscpd_report(report_dir, percentage=0.0, duplicates=[])
    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", _capture)

    asyncio.run(
        DuplicationRunner(report_dir=report_dir, ignore=["**/tests/**", "**/*_test.rs"])
        .run(tmp_path, [])
    )

    ignore_arg = captured["cmd"][captured["cmd"].index("--ignore") + 1]
    assert "**/tests/**" in ignore_arg
    assert "**/*_test.rs" in ignore_arg
    assert "**/node_modules/**" in ignore_arg      # defaults continuam


def test_duplication_suggests_ignoring_tests_when_clones_are_mostly_there(tmp_path, monkeypatch):
    changed = tmp_path / "src" / "lib.rs"
    changed.parent.mkdir()
    changed.write_text("fn main() {}", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    clones = [
        _clone(str(tests_dir / f"caso_{i}.rs"), str(tests_dir / f"caso_{i+1}.rs"))
        for i in range(24)
    ]
    clones.append(_clone(str(changed), str(changed)))
    report_dir = tmp_path.parent / "reports-tests"
    _write_jscpd_report(report_dir, percentage=25.85, duplicates=clones)

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", _successful_process)

    result = asyncio.run(
        DuplicationRunner(max_percent=5, report_dir=report_dir).run(tmp_path, ["src/lib.rs"])
    )
    note = result.metrics["note"]

    assert "clones" in note
    assert "teste" in note
    assert "duplication.ignore" in note
    assert "**/tests/**" in note


def test_duplication_does_not_suggest_when_clones_are_in_production_code(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    clones = [_clone(str(src / f"m{i}.rs"), str(src / f"m{i+1}.rs")) for i in range(24)]
    report_dir = tmp_path.parent / "reports-prod"
    _write_jscpd_report(report_dir, percentage=25.85, duplicates=clones)

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", _successful_process)

    result = asyncio.run(
        DuplicationRunner(max_percent=5, report_dir=report_dir).run(tmp_path, ["src/m0.rs"])
    )

    assert "duplication.ignore" not in result.metrics.get("note", "")
