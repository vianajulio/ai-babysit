from settings import Settings, _deep_merge, settings


def test_load_quality_gate_config_merges_project_babysit_yml(tmp_path):
    (tmp_path / ".babysit.yml").write_text(
        """
quality_gate:
  checks:
    big_o:
      max_files_per_run: 5
    duplication:
      max_percent: 12
      fail_only_on_changed_files: false
    file_size:
      max_lines_per_file: 120
""".strip(),
        encoding="utf-8",
    )

    config = settings.load_quality_gate_config(tmp_path)
    checks = config["quality_gate"]["checks"]

    assert checks["duplication"]["enabled"] is True
    assert checks["duplication"]["max_percent"] == 12
    assert checks["duplication"]["fail_only_on_changed_files"] is False
    assert checks["file_size"]["max_lines_per_file"] == 120
    assert checks["file_size"]["max_lines_per_function"] == 80
    assert checks["big_o"]["enabled"] is False
    assert checks["big_o"]["languages"] == ["csharp"]
    assert checks["big_o"]["max_files_per_run"] == 5


def test_default_quality_gate_is_local_and_ai_opt_in():
    config = settings.quality_gate_config["quality_gate"]

    assert config["provider"] == "local"
    assert config["ratchet"] is False
    assert config["checks"]["big_o"]["enabled"] is False
    assert config["checks"]["ai_review"]["enabled"] is False
    assert not config["workspace"]["temp_dir"].startswith(("/", "\\"))


def test_deep_merge_concatenates_lists_with_dedup():
    config = _deep_merge(
        {"file_size": {"exclude": ["*Migrations*", "*.Designer.cs"]}},
        {"file_size": {"exclude": ["*Templates*", "*Migrations*"]}},
    )

    assert config["file_size"]["exclude"] == [
        "*Migrations*",
        "*.Designer.cs",
        "*Templates*",
    ]


def test_quality_gate_config_exposes_pr_review_defaults():
    cfg = Settings().load_quality_gate_config()
    pr = cfg["quality_gate"]["pr_review"]

    assert pr["one_shot_max_files"] == 10
    assert pr["one_shot_max_diff_lines"] == 800
    assert pr["max_files_per_shard"] == 15
    assert pr["parallel_hint"] == 4


def test_load_quality_gate_config_overrides_pr_review_key(tmp_path):
    (tmp_path / ".babysit.yml").write_text(
        """
quality_gate:
  pr_review:
    one_shot_max_files: 30
""".strip(),
        encoding="utf-8",
    )

    config = settings.load_quality_gate_config(tmp_path)
    pr = config["quality_gate"]["pr_review"]

    assert pr["one_shot_max_files"] == 30
    assert pr["one_shot_max_diff_lines"] == 800
    assert pr["max_files_per_shard"] == 15
    assert pr["parallel_hint"] == 4
    assert pr["max_diff_lines_per_shard"] == 1200
    assert pr["plan_ttl_minutes"] == 60


def test_load_quality_gate_config_extends_file_size_exclude(tmp_path):
    (tmp_path / ".babysit.yml").write_text(
        """
quality_gate:
  checks:
    file_size:
      exclude:
        - "*Migrations*"
""".strip(),
        encoding="utf-8",
    )

    config = settings.load_quality_gate_config(tmp_path)

    assert config["quality_gate"]["checks"]["file_size"]["exclude"] == ["*Migrations*"]


def test_quality_gate_config_exposes_agent_review_defaults():
    cfg = Settings().load_quality_gate_config()
    checks = cfg["quality_gate"]["checks"]
    pr = cfg["quality_gate"]["pr_review"]

    assert checks["agent_review"] == {"enabled": False, "block_on": "high"}
    assert pr["max_context_chars"] == 120000
    assert pr["max_files_per_review_task"] == 6
    assert pr["max_diff_lines_per_review_task"] == 400


def test_project_config_can_enable_agent_review(tmp_path):
    (tmp_path / ".babysit.yml").write_text(
        """
quality_gate:
  checks:
    agent_review:
      enabled: true
      block_on: none
""".strip(),
        encoding="utf-8",
    )

    config = settings.load_quality_gate_config(tmp_path)
    checks = config["quality_gate"]["checks"]

    assert checks["agent_review"]["enabled"] is True
    assert checks["agent_review"]["block_on"] == "none"
    assert checks["file_size"]["enabled"] is True


def test_standards_are_read_from_the_reviewed_workspace(tmp_path):
    from pathlib import Path

    from app.ai import prompts

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "coding-standards.md").write_text("REGRAS DO PROJETO", encoding="utf-8")

    assert prompts.load_standards(Path(tmp_path)) == "REGRAS DO PROJETO"
    assert set(prompts.FINDING_SCHEMA) == {
        "file", "line", "severity", "category", "message", "suggestion",
    }
