from app.runners.line_count import count_lines, strip_test_blocks

PY = """\
# comentário de cabeçalho
import os


def soma(a, b):
    \"\"\"Soma dois números.

    Documentação longa não deve inflar o tamanho do arquivo.
    \"\"\"
    return a + b
"""

RS = """\
// comentário
pub fn soma(a: i32, b: i32) -> i32 {
    a + b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn soma_funciona() {
        assert_eq!(soma(1, 2), 3);
    }
}
"""


def test_raw_mode_counts_every_line():
    assert count_lines(PY, "python", mode="raw") == len(PY.splitlines())


def test_code_mode_ignores_blank_lines_and_comments():
    # import + def + return = 3 linhas de código
    assert count_lines(PY, "python", mode="code") == 3


def test_code_mode_ignores_python_docstrings():
    assert "Documentação longa" in PY
    assert count_lines(PY, "python", mode="code") < count_lines(PY, "python", mode="raw")


def test_code_mode_ignores_block_comments():
    source = "int a = 1;\n/* bloco\n   de comentário */\nint b = 2;\n"

    assert count_lines(source, "csharp", mode="code") == 2


def test_code_mode_keeps_string_that_looks_like_a_comment():
    source = 'url = "http://exemplo.com"  # comentário de verdade\noutra = 1\n'

    assert count_lines(source, "python", mode="code") == 2


def test_strip_test_blocks_removes_rust_inline_tests():
    stripped = strip_test_blocks(RS, "rust")

    assert "assert_eq!" not in stripped
    assert "pub fn soma" in stripped


def test_strip_test_blocks_is_a_noop_for_languages_without_inline_tests():
    assert strip_test_blocks(PY, "python") == PY


def test_rust_file_shrinks_when_tests_are_excluded():
    with_tests = count_lines(RS, "rust", mode="code")
    without = count_lines(strip_test_blocks(RS, "rust"), "rust", mode="code")

    assert without < with_tests
