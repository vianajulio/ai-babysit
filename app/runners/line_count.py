"""Contagem de linhas de um arquivo, por linha bruta ou só por código.

Contar linha bruta pune quem documenta: um arquivo com 120 linhas de docstring
"estoura" igual a um com 120 linhas de lógica, e o jeito mais rápido de passar
no gate vira apagar comentário. O modo `code` ignora linha em branco,
comentário e docstring, e opcionalmente o bloco de teste que algumas
linguagens mantêm no mesmo arquivo.
"""
from __future__ import annotations

import re

# Marcador de comentário de linha por linguagem; o que não estiver aqui usa a
# forma mais comum (`//`).
_LINE_COMMENT: dict[str, tuple[str, ...]] = {
    "python": ("#",),
    "ruby": ("#",),
    "csharp": ("//",),
    "typescript": ("//",),
    "javascript": ("//",),
    "go": ("//",),
    "java": ("//",),
    "kotlin": ("//",),
    "rust": ("//",),
    "c": ("//",),
    "cpp": ("//",),
}

# Linguagens cujo bloco de comentário multi-linha é /* ... */.
_BLOCK_COMMENT = {
    "csharp", "typescript", "javascript", "go", "java", "kotlin", "rust", "c", "cpp",
}

_PY_DOCSTRING = re.compile(r'^(?:[rubf]{0,2})("""|\'\'\')')

# `#[cfg(test)] mod tests { … }`: convenção do Rust de manter o teste no mesmo
# arquivo. Contá-lo no tamanho pune quem escreve teste.
_RUST_TEST_ATTR = re.compile(r"^\s*#\[cfg\(test\)\]")


def _line_comment_prefixes(language: str) -> tuple[str, ...]:
    return _LINE_COMMENT.get(language, ("//",))


def strip_test_blocks(source: str, language: str) -> str:
    """Remove blocos de teste que vivem dentro do arquivo de produção.

    Hoje só o Rust tem essa convenção entre as linguagens suportadas; para as
    demais devolve o texto intacto.
    """
    if language != "rust":
        return source

    lines = source.splitlines(keepends=True)
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if not _RUST_TEST_ATTR.match(lines[index]):
            kept.append(lines[index])
            index += 1
            continue

        # Pula o atributo e o bloco que ele anota, casando as chaves.
        depth = 0
        started = False
        while index < len(lines):
            depth += lines[index].count("{") - lines[index].count("}")
            started = started or "{" in lines[index]
            index += 1
            if started and depth <= 0:
                break
    return "".join(kept)


def _is_code_line(line: str, language: str, state: dict) -> bool:
    """Decide se a linha conta como código, carregando estado entre chamadas."""
    stripped = line.strip()

    if state.get("docstring"):
        if state["docstring"] in stripped:
            state["docstring"] = None
        return False

    if state.get("block_comment"):
        if "*/" in stripped:
            state["block_comment"] = False
            after = stripped.split("*/", 1)[1].strip()
            return bool(after)
        return False

    if not stripped:
        return False

    if any(stripped.startswith(prefix) for prefix in _line_comment_prefixes(language)):
        return False

    if language == "python":
        match = _PY_DOCSTRING.match(stripped)
        if match:
            quote = match.group(1)
            rest = stripped[match.end():]
            # Docstring de uma linha só fecha na própria linha.
            if quote not in rest:
                state["docstring"] = quote
            return False

    if language in _BLOCK_COMMENT and stripped.startswith("/*"):
        if "*/" not in stripped:
            state["block_comment"] = True
            return False
        return bool(stripped.split("*/", 1)[1].strip())

    return True


def count_lines(source: str, language: str, *, mode: str = "code") -> int:
    """Número de linhas do arquivo no modo pedido.

    `raw` conta tudo (comportamento histórico); `code` conta só o que é
    instrução — é a medida que corresponde ao custo real de ler o arquivo.
    """
    if mode not in ("code", "raw"):
        raise ValueError(f"count_mode inválido: {mode!r}; use 'code' ou 'raw'")

    lines = source.splitlines()
    if mode == "raw":
        return len(lines)

    state: dict = {}
    return sum(1 for line in lines if _is_code_line(line, language, state))
