from app.gates.orchestrator import _build_runners
from app.runners.big_o import BigORunner


def test_build_runners_registers_big_o_when_enabled():
    runners = _build_runners({
        "quality_gate": {
            "checks": {
                "file_size": {"enabled": False},
                "complexity": {"enabled": False},
                "duplication": {"enabled": False},
                "secrets": {"enabled": False},
                "big_o": {
                    "enabled": True,
                    "languages": ["csharp"],
                    "fail_on_high": False,
                    "warn_on_medium": True,
                    "max_files_per_run": 7,
                    "max_code_chars": 900,
                },
            }
        }
    })

    assert len(runners) == 1
    assert isinstance(runners[0], BigORunner)
    assert runners[0].fail_on_high is False
    assert runners[0].max_files_per_run == 7
    assert runners[0].max_code_chars == 900


def test_build_runners_excludes_big_o_when_disabled():
    runners = _build_runners({
        "quality_gate": {
            "checks": {
                "file_size": {"enabled": False},
                "complexity": {"enabled": False},
                "duplication": {"enabled": False},
                "secrets": {"enabled": False},
                "big_o": {"enabled": False},
            }
        }
    })

    assert not any(isinstance(runner, BigORunner) for runner in runners)
