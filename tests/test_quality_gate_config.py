from settings import settings


def test_load_quality_gate_config_merges_project_babysit_yml(tmp_path):
    (tmp_path / ".babysit.yml").write_text(
        """
quality_gate:
  checks:
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
