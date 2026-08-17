"""Extração de arquivos alterados e peso (linhas adicionadas) via git.

Reutilizado por `_commit_files` (mcp_server_standalone.py) e pelo planejador
de revisão de PR em partes.
"""
import subprocess
from pathlib import Path

from pydantic import BaseModel


class ChangedFile(BaseModel):
    path: str
    added_lines: int


def run_git(
    args: list[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    allow_failure: bool = False,
) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            input=input_text,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git não está instalado") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("comando git excedeu o timeout") from exc

    if completed.returncode != 0 and not allow_failure:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git falhou: {detail[-500:]}")
    return completed.stdout


def resolve_commit(repo_root: Path, ref: str) -> str:
    """Resolve `ref` para o SHA do commit correspondente.

    Quando `ref` é o pai de um commit raiz (ex.: ``"<sha>^"`` de um commit sem
    pai), devolve o SHA da árvore vazia do Git em vez de propagar o erro, para
    que o diff continue funcionando no primeiro commit do repositório.
    """
    try:
        return run_git(["rev-parse", "--verify", f"{ref.strip()}^{{commit}}"], repo_root).strip()
    except RuntimeError:
        if ref.strip().endswith("^"):
            return run_git(["hash-object", "-t", "tree", "--stdin"], repo_root, input_text="").strip()
        raise


def changed_files_with_weight(repo_root: Path, base: str, head: str) -> list[ChangedFile]:
    """Lista os arquivos alterados entre `base` e `head` com o peso (linhas
    adicionadas) de cada um, usado para balancear os shards da revisão em
    partes. Arquivos binários (marcados com ``-`` pelo git) recebem
    `added_lines=0`."""
    output = run_git(
        ["diff", "--numstat", "-z", "--no-renames", "--diff-filter=ACMRT", base, head],
        repo_root,
    )

    changed_files: list[ChangedFile] = []
    for record in output.split("\0"):
        if not record:
            continue
        added_str, _removed_str, path_str = record.split("\t", 2)
        path = Path(path_str)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"caminho git inválido: {path_str}")
        added_lines = 0 if added_str == "-" else int(added_str)
        changed_files.append(ChangedFile(path=path.as_posix(), added_lines=added_lines))
    return changed_files
