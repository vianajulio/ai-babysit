import asyncio
import subprocess

from app.workspaces import manager


def run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_create_workspace_resolves_relative_temp_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(manager.settings, "babysit_temp_dir", ".babysit/workspaces")

    workspace = manager.create_workspace("run-1")

    assert workspace == (tmp_path / ".babysit" / "workspaces" / "run-1" / "repo").resolve()
    manager.cleanup_workspace(workspace)


def test_prepare_workspace_checks_out_fetched_branch(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    run_git(["init", "-b", "main"], source)
    (source / "file.txt").write_text("main\n")
    run_git(["add", "file.txt"], source)
    run_git(["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "main"], source)
    run_git(["checkout", "-b", "feature/conta-download-export-zip"], source)
    (source / "file.txt").write_text("feature\n")
    run_git(["add", "file.txt"], source)
    run_git(["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "feature"], source)
    run_git(["checkout", "main"], source)

    monkeypatch.setattr(manager.settings, "babysit_temp_dir", str(tmp_path / "workspaces"))
    workspace = asyncio.run(
        manager.prepare_workspace(
            "run-1",
            str(source),
            "feature/conta-download-export-zip",
        )
    )

    assert (workspace / "file.txt").read_text() == "feature\n"
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert branch.stdout.strip() == "feature/conta-download-export-zip"
