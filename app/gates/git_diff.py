"""Extração de arquivos alterados e peso (linhas adicionadas) via git.

Reutilizado por `_commit_files` (mcp_server_standalone.py) e pelo planejador
de revisão de PR em partes.
"""
import subprocess
from pathlib import Path

from pydantic import BaseModel


# Sentinela aceita no lugar de `head`: compara `base` com a árvore de trabalho
# atual (incluindo arquivos não rastreados) em vez de com um commit.
WORKTREE_REF = "WORKTREE"


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


def _safe_relative_path(path_str: str) -> str:
    path = Path(path_str)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"caminho git inválido: {path_str}")
    return path.as_posix()


def _untracked_files(repo_root: Path) -> list[ChangedFile]:
    """Arquivos novos ainda não rastreados, com o próprio tamanho como peso.

    `git diff` não os enxerga; numa revisão de árvore de trabalho eles costumam
    ser justamente o código novo que mais interessa revisar.
    """
    output = run_git(["ls-files", "--others", "--exclude-standard", "-z"], repo_root)
    files: list[ChangedFile] = []
    for path_str in output.split("\0"):
        if not path_str:
            continue
        relative = _safe_relative_path(path_str)
        try:
            content = (repo_root / relative).read_text(encoding="utf-8", errors="ignore")
            added_lines = len(content.splitlines())
        except OSError:
            added_lines = 0
        files.append(ChangedFile(path=relative, added_lines=added_lines))
    return files


def _synthetic_diff(repo_root: Path, path: str) -> str:
    """Diff de um arquivo não rastreado, que `git diff` não enxerga.

    Formato compatível com o que um revisor espera ler (cabeçalho + linhas
    prefixadas por ``+``), sem depender de `--no-index`, que se comporta de
    forma diferente entre plataformas.
    """
    try:
        content = (repo_root / path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"


def file_diffs(
    repo_root: Path,
    base: str,
    head: str,
    paths: list[str],
    *,
    context_lines: int = 3,
) -> dict[str, str]:
    """Diff unificado por arquivo, na ordem de `paths`.

    Um `git diff` por arquivo (e não um diff único fatiado depois) porque a
    fatia de uma review task é pequena por construção e o corte por arquivo
    precisa ser exato para o orçamento de contexto.
    """
    worktree_mode = head == WORKTREE_REF
    diffs: dict[str, str] = {}
    for path in paths:
        # `paths` chega do payload do plano (persistido), não direto do git:
        # revalidar aqui impede que um registro adulterado faça o diff — ou o
        # fallback que lê o arquivo — sair da raiz do repositório.
        path = _safe_relative_path(path)
        args = ["diff", f"--unified={context_lines}", "--no-renames", base]
        if not worktree_mode:
            args.append(head)
        args.extend(["--", path])
        output = run_git(args, repo_root, allow_failure=True)
        if not output.strip() and worktree_mode:
            output = _synthetic_diff(repo_root, path)
        diffs[path] = output
    return diffs


def changed_files_with_weight(repo_root: Path, base: str, head: str) -> list[ChangedFile]:
    """Lista os arquivos alterados entre `base` e `head` com o peso (linhas
    adicionadas) de cada um, usado para balancear os shards da revisão em
    partes. Arquivos binários (marcados com ``-`` pelo git) recebem
    `added_lines=0`.

    Com `head == WORKTREE_REF`, compara `base` com a árvore de trabalho atual e
    inclui os arquivos não rastreados.
    """
    worktree_mode = head == WORKTREE_REF
    diff_args = ["diff", "--numstat", "-z", "--no-renames", "--diff-filter=ACMRT", base]
    if not worktree_mode:
        diff_args.append(head)
    output = run_git(diff_args, repo_root)

    changed_files: list[ChangedFile] = []
    for record in output.split("\0"):
        if not record:
            continue
        added_str, _removed_str, path_str = record.split("\t", 2)
        added_lines = 0 if added_str == "-" else int(added_str)
        changed_files.append(
            ChangedFile(path=_safe_relative_path(path_str), added_lines=added_lines)
        )

    if worktree_mode:
        known = {file.path for file in changed_files}
        changed_files.extend(
            file for file in _untracked_files(repo_root) if file.path not in known
        )
    return changed_files
