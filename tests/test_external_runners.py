import asyncio
import json

from app.gates.models import GateStatus
from app.runners.complexity import ComplexityRunner
from app.runners.duplication import DuplicationRunner
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
    _write_jscpd_report(
        tmp_path.parent,
        percentage=27.5,
        duplicates=[_clone(str(unchanged), str(unchanged))],
    )

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", _successful_process)

    result = asyncio.run(
        DuplicationRunner(max_percent=5, fail_only_on_changed_files=True).run(tmp_path, ["Changed.cs"])
    )

    assert result.status == GateStatus.passed
    assert result.metrics["duplication_percent"] == 27.5
    assert result.violations == []


def test_duplication_runner_fails_when_changed_file_has_duplication(tmp_path, monkeypatch):
    changed = tmp_path / "Changed.cs"
    other = tmp_path / "Other.cs"
    changed.write_text("public class Changed {}", encoding="utf-8")
    other.write_text("public class Other {}", encoding="utf-8")
    _write_jscpd_report(
        tmp_path.parent,
        percentage=27.5,
        duplicates=[_clone(str(changed), str(other))],
    )

    monkeypatch.setattr("app.runners.duplication.asyncio.create_subprocess_exec", _successful_process)

    result = asyncio.run(
        DuplicationRunner(max_percent=5, fail_only_on_changed_files=True).run(tmp_path, ["Changed.cs"])
    )

    assert result.status == GateStatus.failed
    assert result.violations[0].file == "Changed.cs"


def test_secrets_runner_skips_when_gitleaks_is_missing(tmp_path, monkeypatch):
    async def missing_command(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("app.runners.secrets.asyncio.create_subprocess_exec", missing_command)

    result = asyncio.run(SecretsRunner().run(tmp_path, []))

    assert result.status == GateStatus.skipped
    assert result.metrics == {"error": "gitleaks não instalado"}


class _Process:
    async def communicate(self):
        return b"", b""

    def kill(self):
        pass


async def _successful_process(*args, **kwargs):
    return _Process()


def _write_jscpd_report(report_dir, percentage: float, duplicates: list[dict]):
    (report_dir / "jscpd-report.json").write_text(
        json.dumps({"statistics": {"total": {"percentage": percentage}}, "duplicates": duplicates}),
        encoding="utf-8",
    )


def _clone(source_a: str, source_b: str) -> dict:
    return {
        "duplicationA": [{"sourceId": source_a, "start": {"line": 1}}],
        "duplicationB": [{"sourceId": source_b, "start": {"line": 1}}],
    }
