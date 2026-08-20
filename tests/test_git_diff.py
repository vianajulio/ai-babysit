import subprocess
from pathlib import Path

import pytest

from app.gates import git_diff


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(tmp_path: Path) -> Path:
    """git init + dois commits: base (README.md) e um commit que adiciona
    ``src/foo.py`` com 12 linhas."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["init", "-b", "main"], repo)
    _run(["config", "user.email", "test@example.com"], repo)
    _run(["config", "user.name", "Test"], repo)

    (repo / "README.md").write_text("base\n")
    _run(["add", "README.md"], repo)
    _run(["commit", "-m", "base"], repo)

    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "foo.py").write_text("\n".join(f"line {i}" for i in range(12)) + "\n")
    _run(["add", "src/foo.py"], repo)
    _run(["commit", "-m", "add foo"], repo)

    return repo


def test_changed_files_with_weight_between_refs(tmp_path):
    repo = _init_repo(tmp_path)
    files = git_diff.changed_files_with_weight(repo, base="HEAD~1", head="HEAD")

    assert files == [git_diff.ChangedFile(path="src/foo.py", added_lines=12)]


def test_changed_files_rejects_paths_outside_repo(tmp_path, monkeypatch):
    # mesma validação de `_commit_files`: nada absoluto, nada com ".."
    repo = _init_repo(tmp_path)

    def fake_run_git(args, cwd, **kwargs):
        assert "diff" in args
        return "1\t0\t../outside.py\x00"

    monkeypatch.setattr(git_diff, "run_git", fake_run_git)

    with pytest.raises(ValueError):
        git_diff.changed_files_with_weight(repo, base="HEAD~1", head="HEAD")


def test_file_diffs_rejects_paths_outside_the_repo(tmp_path):
    import pytest

    from app.gates import git_diff

    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match="caminho git inválido"):
        git_diff.file_diffs(repo, "HEAD", git_diff.WORKTREE_REF, ["../../etc/passwd"])
