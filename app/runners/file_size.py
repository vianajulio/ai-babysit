import fnmatch
import re
from pathlib import Path

from app.gates.models import CheckResult, GateStatus, Violation

_FUNC_PATTERNS = [
    # C#, Java, Go, Kotlin
    re.compile(r"^\s*(public|private|protected|internal|static|async|override|virtual).*\("),
    # Python
    re.compile(r"^\s*def "),
    # JS/TS
    re.compile(r"^\s*(function |const \w+ ?= ?(\(|async))"),
]

_IGNORED_EXTENSIONS = {
    ".html", ".md", ".txt", ".json", ".yaml", ".yml", ".lock", ".sum", ".xml", ".csproj", ".sln"
}
_IGNORED_SUFFIXES = (".designer.cs", ".generated.cs", ".g.cs")
_CS_TYPE_DECLARATIONS = re.compile(r"\b(class|record|struct|interface|enum)\b")


def _is_function_start(line: str) -> bool:
    if not any(p.match(line) for p in _FUNC_PATTERNS):
        return False
    return not _CS_TYPE_DECLARATIONS.search(line)


def _is_ignored_file(path: str) -> bool:
    lower_path = path.lower()
    return Path(path).suffix.lower() in _IGNORED_EXTENSIONS or lower_path.endswith(_IGNORED_SUFFIXES)


def _matches_any(rel_path: str, patterns: list[str]) -> bool:
    normalized = rel_path.lstrip("/").replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def _count_function_lines(lines: list[str], max_lines: int) -> list[tuple[int, int]]:
    """Returns list of (start_line, length) for functions exceeding max_lines."""
    violations = []
    in_func = False
    func_start = 0
    brace_depth = 0

    for i, line in enumerate(lines, start=1):
        if not in_func and _is_function_start(line):
            in_func = True
            func_start = i
            brace_depth = line.count("{") - line.count("}")
            continue

        if in_func:
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0:
                length = i - func_start + 1
                if length > max_lines:
                    violations.append((func_start, length))
                in_func = False
                brace_depth = 0

    return violations


class FileSizeRunner:
    name = "file_size"

    def __init__(
        self,
        max_lines_per_file: int = 300,
        max_lines_per_function: int = 80,
        exclude: list[str] | None = None,
        new_files: set[str] | None = None,
        fail_on_existing_files: bool = False,
    ):
        self.max_lines_per_file = max_lines_per_file
        self.max_lines_per_function = max_lines_per_function
        self.exclude = exclude or []
        # Arquivos criados por esta mudança. `None` significa "origem
        # desconhecida": nesse caso todo arquivo é tratado como novo, para o
        # gate nunca afrouxar por falta de informação.
        self.new_files = new_files
        self.fail_on_existing_files = fail_on_existing_files

    def _is_new(self, rel_path: str) -> bool:
        if self.new_files is None:
            return True
        return rel_path.lstrip("/").replace("\\", "/") in self.new_files

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        violations: list[Violation] = []
        max_file_lines = 0
        max_function_lines = 0

        for rel_path in changed_files:
            if _is_ignored_file(rel_path):
                continue
            if _matches_any(rel_path, self.exclude):
                continue

            full_path = workspace / rel_path.lstrip("/")
            if not full_path.exists():
                continue

            try:
                lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            max_file_lines = max(max_file_lines, len(lines))

            is_new = self._is_new(rel_path)
            origin = "" if is_new else " — arquivo já existente antes desta mudança"

            if len(lines) > self.max_lines_per_file:
                violations.append(Violation(
                    file=rel_path,
                    severity="high" if is_new else "medium",
                    message=(
                        f"Arquivo com {len(lines)} linhas "
                        f"(limite: {self.max_lines_per_file}){origin}"
                    ),
                    current_value=len(lines),
                    allowed_value=self.max_lines_per_file,
                ))

            function_violations = _count_function_lines(lines, self.max_lines_per_function)
            for _, length in function_violations:
                max_function_lines = max(max_function_lines, length)

            for start, length in function_violations:
                violations.append(Violation(
                    file=rel_path,
                    line=start,
                    severity="high" if is_new else "low",
                    message=(
                        f"Função com {length} linhas "
                        f"(limite: {self.max_lines_per_function}){origin}"
                    ),
                    current_value=length,
                    allowed_value=self.max_lines_per_function,
                ))

        # Arquivo que o PR criou reprova; arquivo que já era grande e foi
        # apenas encostado vira aviso — senão todo PR que toca um legado herda
        # a dívida inteira dele. `fail_on_existing_files` traz o rigor antigo.
        new_violations = [v for v in violations if self._is_new(v.file)]
        metrics = {
            "violations_count": len(violations),
            "new_file_violations": len(new_violations),
            "max_file_lines": max_file_lines,
            "max_function_lines": max_function_lines,
        }

        if not violations:
            status = GateStatus.passed
        elif new_violations or self.fail_on_existing_files:
            status = GateStatus.failed
        else:
            status = GateStatus.warning
            metrics["note"] = (
                f"{len(violations)} violação(ões) apenas em arquivos que já existiam "
                "antes desta mudança; use fail_on_existing_files para reprovar também"
            )

        return CheckResult(
            check=self.name,
            status=status,
            metrics=metrics,
            violations=violations,
        )
