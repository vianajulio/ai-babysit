"""Resolução de workspace, arquivos e worktrees temporários."""
import ntpath
import shutil
import subprocess
from pathlib import Path

from app.gates import git_diff


def auto_repository(workspace: str, repository: str) -> str:
    """Deriva o identificador do repositório a partir do caminho relativo enxuto
    do workspace (dois últimos componentes, ex.: ``energia/backend``) quando o
    chamador não informa um valor explícito. Evita que projetos diferentes
    colidam na mesma linha de baseline ao compartilhar o mesmo banco."""
    if repository and repository != "local":
        return repository
    parts = Path(workspace).resolve().parts
    relative = "/".join(parts[-2:]).lstrip("/")
    return relative or "local"


def auto_branch(workspace: str, branch: str) -> str:
    """Deriva a branch atual via git quando não informada explicitamente."""
    if branch and branch != "local":
        return branch
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(workspace).resolve()), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        detected = out.stdout.strip()
        if out.returncode == 0 and detected and detected != "HEAD":
            return detected
    except Exception:
        pass
    return "local"


def resolve_local_files(workspace: Path, files: list[str]) -> list[str]:
    """Resolve input files and return safe workspace-relative paths.

    A leading slash is retained for compatibility with the runners' old
    ``lstrip('/')`` behavior when it does not identify an existing external
    file.  Existing absolute paths outside the workspace are always rejected.
    """
    root = workspace.resolve()
    resolved_files: list[str] = []
    missing: list[str] = []
    outside: list[str] = []

    for file_name in files:
        text = str(file_name)
        candidate = Path(text)
        resolved: Path

        rooted_path = candidate.is_absolute() or text.startswith(("/", "\\"))
        if rooted_path:
            absolute_candidate = candidate.resolve()
            if absolute_candidate.exists():
                resolved = absolute_candidate
            elif ntpath.splitdrive(text)[0] or text.startswith("\\\\"):
                missing.append(text)
                continue
            else:
                # ``/Foo.py`` has historically meant ``workspace/Foo.py`` in
                # the local MCP API.  Only use that compatibility form when
                # the actual absolute path does not exist.
                resolved = (root / text.lstrip("/\\")).resolve()
        else:
            resolved = (root / candidate).resolve()

        try:
            relative = resolved.relative_to(root)
        except ValueError:
            outside.append(text)
            continue

        if not resolved.exists():
            missing.append(relative.as_posix())
        elif not resolved.is_file():
            missing.append(relative.as_posix())
        else:
            resolved_files.append(relative.as_posix())

    if outside:
        raise ValueError(f"arquivos fora do workspace: {outside}")
    if missing:
        raise ValueError(f"arquivos não encontrados no workspace: {missing}")
    return resolved_files


# `run_git` mora em app/gates/git_diff.py; mantido aqui sob o nome antigo
# porque os demais usos neste arquivo (worktree add/remove) ainda chamam `_git`.
git = git_diff.run_git


def commit_files(repo_root: Path, sha: str, base_commit: str | None) -> tuple[str, list[str]]:
    if not sha or not sha.strip():
        raise ValueError("SHA do commit é obrigatório")

    resolved_sha = git_diff.resolve_commit(repo_root, sha)
    resolved_base = git_diff.resolve_commit(repo_root, base_commit if base_commit else f"{resolved_sha}^")

    changed_files = [
        file.path for file in git_diff.changed_files_with_weight(repo_root, resolved_base, resolved_sha)
    ]
    return resolved_sha, changed_files


def cleanup_worktree(repo_root: Path, worktree: Path | None, temp_root: Path | None) -> None:
    if worktree is not None:
        try:
            git(["worktree", "remove", "--force", str(worktree)], repo_root, allow_failure=True)
        except Exception:
            # Filesystem cleanup below is still required if Git itself is
            # unavailable or cannot unregister a partially-created worktree.
            pass
    if temp_root is not None:
        shutil.rmtree(temp_root, ignore_errors=True)

