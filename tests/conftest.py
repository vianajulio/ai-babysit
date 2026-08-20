"""Higiene de arquivos temporários da suíte.

Vários testes chamam `plan_pr_review` de verdade, que cria um worktree em
`%TEMP%/babysit-review-plan-*`. Como poucos chamam `close_review_plan` no fim,
a suíte deixava centenas de diretórios órfãos por execução. A limpeza vive
aqui, e não em cada teste, para não misturar assunto com o que cada um mede.
"""
import shutil
import tempfile
from pathlib import Path

import pytest

_PREFIXES = ("babysit-review-plan-", "babysit-commit-gate-")


def _temp_dirs() -> set[Path]:
    root = Path(tempfile.gettempdir())
    return {
        path
        for prefix in _PREFIXES
        for path in root.glob(f"{prefix}*")
        if path.is_dir()
    }


@pytest.fixture(autouse=True)
def _cleanup_babysit_temp_dirs():
    before = _temp_dirs()
    yield
    for path in _temp_dirs() - before:
        shutil.rmtree(path, ignore_errors=True)
