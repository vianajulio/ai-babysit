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


def test_run_git_accepts_input_text_without_stdin_conflict(tmp_path):
    from app.gates import git_diff

    empty_tree = git_diff.run_git(
        ["hash-object", "-t", "tree", "--stdin"], tmp_path, input_text=""
    ).strip()

    assert len(empty_tree) == 40


def _repo(tmp_path):
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@e.invalid")
    run("config", "user.name", "T")
    (repo / "a.py").write_text("v = 1\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "base")
    return repo


def test_worktree_diff_does_not_invent_a_synthetic_diff_for_tracked_files(tmp_path):
    from app.gates import git_diff

    repo = _repo(tmp_path)          # a.py rastreado e sem alteração

    diffs = git_diff.file_diffs(repo, "HEAD", git_diff.WORKTREE_REF, ["a.py"])

    assert diffs["a.py"] == ""      # nada mudou; não é código novo


def test_untracked_binary_file_weighs_zero_and_has_no_diff_body(tmp_path):
    from app.gates import git_diff

    repo = _repo(tmp_path)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)

    weights = {f.path: f.added_lines for f in git_diff.changed_files_with_weight(
        repo, "HEAD", git_diff.WORKTREE_REF
    )}
    diffs = git_diff.file_diffs(repo, "HEAD", git_diff.WORKTREE_REF, ["blob.bin"])

    assert weights["blob.bin"] == 0
    assert "Binary file" in diffs["blob.bin"]
