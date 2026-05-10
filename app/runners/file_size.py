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

_IGNORED_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".lock", ".sum", ".xml", ".csproj", ".sln"}
_CS_TYPE_DECLARATIONS = re.compile(r"\b(class|record|struct|interface|enum)\b")


def _is_function_start(line: str) -> bool:
    if not any(p.match(line) for p in _FUNC_PATTERNS):
        return False
    return not _CS_TYPE_DECLARATIONS.search(line)


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

    def __init__(self, max_lines_per_file: int = 400, max_lines_per_function: int = 80):
        self.max_lines_per_file = max_lines_per_file
        self.max_lines_per_function = max_lines_per_function

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        violations: list[Violation] = []
        max_file_lines = 0
        max_function_lines = 0

        for rel_path in changed_files:
            if Path(rel_path).suffix.lower() in _IGNORED_EXTENSIONS:
                continue

            full_path = workspace / rel_path.lstrip("/")
            if not full_path.exists():
                continue

            try:
                lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            max_file_lines = max(max_file_lines, len(lines))

            if len(lines) > self.max_lines_per_file:
                violations.append(Violation(
                    file=rel_path,
                    severity="medium",
                    message=f"Arquivo com {len(lines)} linhas (limite: {self.max_lines_per_file})",
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
                    severity="low",
                    message=f"Função com {length} linhas (limite: {self.max_lines_per_function})",
                    current_value=length,
                    allowed_value=self.max_lines_per_function,
                ))

        status = GateStatus.failed if violations else GateStatus.passed
        return CheckResult(
            check=self.name,
            status=status,
            metrics={
                "violations_count": len(violations),
                "max_file_lines": max_file_lines,
                "max_function_lines": max_function_lines,
            },
            violations=violations,
        )
