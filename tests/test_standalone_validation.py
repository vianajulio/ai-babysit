import asyncio
import json

import mcp_server_standalone as server


def _call(workspace, files):
    return json.loads(asyncio.run(server.run_local_gate(str(workspace), files)))


def test_run_local_gate_aborts_when_workspace_is_not_a_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    missing_ws = tmp_path / "nope"

    result = _call(missing_ws, ["Foo.py"])

    assert "error" in result
    assert str(missing_ws) in result["error"]


def test_run_local_gate_aborts_when_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))

    result = _call(tmp_path, [])

    assert "error" in result


def test_run_local_gate_aborts_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    (tmp_path / "Foo.py").write_text("x = 1\n")

    result = _call(tmp_path, ["Foo.py", "Missing.py"])

    assert "error" in result
    assert "Missing.py" in result["error"]
    assert "Foo.py" not in result["error"]


def test_run_local_gate_resolves_files_relative_to_workspace(tmp_path, monkeypatch):
    """Leading slash is stripped and joined to workspace, mirroring the runners."""
    monkeypatch.setattr(server, "_ensure_db", lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    (tmp_path / "Foo.py").write_text("x = 1\n")

    result = _call(tmp_path, ["/Foo.py", "/Bar.py"])

    assert "error" in result
    assert "Bar.py" in result["error"]
    assert "Foo.py" not in result["error"]


def test_run_local_gate_passes_validation_when_files_exist(tmp_path, monkeypatch):
    (tmp_path / "Foo.py").write_text("x = 1\n")
    monkeypatch.setattr(server, "_ensure_db", lambda: None)

    captured = {}

    async def fake_gate(**kwargs):
        captured.update(kwargs)
        return {"status": "passed", "checks": []}

    monkeypatch.setattr(server, "run_local_quality_gate", fake_gate)
    monkeypatch.setattr(server, "build_quality_gate_table", lambda result, before: "OK")

    out = asyncio.run(server.run_local_gate(str(tmp_path), ["Foo.py"]))

    assert out == "OK"
    assert captured["changed_files"] == ["Foo.py"]
