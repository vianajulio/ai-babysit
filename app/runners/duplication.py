import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path

from app.gates.models import CheckResult, GateStatus, Violation
from app.runners.tools import resolve_tool


# Diretórios que nunca são código do projeto: ignorá-los não é preferência,
# é o que faz o percentual significar alguma coisa.
_DEFAULT_IGNORE = (
    "**/.venv/**", "**/venv/**", "**/node_modules/**", "**/dist/**", "**/build/**",
)

# Convenções de nome de teste, usadas só para *sugerir* configuração — nunca
# para ignorar por conta própria.
_TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_(test|spec)\.[^/.]+$|"
    r"\.(test|spec)\.[^/.]+$|Tests?\.[^/.]+$",
    re.IGNORECASE,
)

# Abaixo disso o repositório é pequeno demais para a sugestão significar algo.
_MIN_CLONES_TO_SUGGEST = 20
_TEST_CLONE_RATIO = 0.5


def _suggest_ignore(clone_paths: list[str]) -> str | None:
    """Sugere `duplication.ignore` quando os clones são majoritariamente testes.

    Teste tabelado duplica por natureza (mesmo `assert` com dados diferentes), e
    um percentual dominado por eles esconde a duplicação que interessa. O gate
    aponta a chave e o valor; quem edita o `.babysit.yml` é o time, não ele.
    """
    if len(clone_paths) < _MIN_CLONES_TO_SUGGEST:
        return None

    in_tests = [path for path in clone_paths if _TEST_PATH.search(path)]
    ratio = len(in_tests) / len(clone_paths)
    if ratio < _TEST_CLONE_RATIO:
        return None

    return (
        f"{len(clone_paths)} clones, {ratio:.0%} em arquivos de teste. "
        "Para tirá-los da conta, em .babysit.yml: "
        'quality_gate.checks.duplication.ignore: ["**/tests/**", "**/*_test.*", "**/*.spec.*"]'
    )


class DuplicationRunner:
    name = "duplication"

    def __init__(
        self,
        max_percent: float = 5.0,
        fail_only_on_changed_files: bool = False,
        report_dir: Path | None = None,
        ignore: list[str] | None = None,
    ):
        self.max_percent = max_percent
        self.fail_only_on_changed_files = fail_only_on_changed_files
        self.report_dir = report_dir
        # Somado aos defaults, nunca no lugar deles: ignorar `node_modules` não
        # é escolha de projeto, é pré-requisito para o número fazer sentido.
        self.ignore = list(ignore or [])

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        owns_report_dir = self.report_dir is None
        report_dir = self.report_dir or Path(
            tempfile.mkdtemp(prefix="jscpd-", dir=str(workspace.parent))
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "jscpd-report.json"

        try:
            cmd = [
                resolve_tool("jscpd"), workspace.as_posix(),
                "--reporters", "json",
                "--output", str(report_dir).replace("\\", "/"),
                "--silent",
                "--ignore", ",".join([*_DEFAULT_IGNORE, *self.ignore]),
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    # Sob o servidor MCP o stdin do processo é o pipe do
                    # cliente stdio; herdá-lo trava a ferramenta até o timeout.
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                return CheckResult(check=self.name, status=GateStatus.error,
                                   metrics={"error": "jscpd timeout"})
            except FileNotFoundError:
                return CheckResult(check=self.name, status=GateStatus.skipped,
                                   metrics={"error": "jscpd não instalado"})

            if not report_path.exists():
                return CheckResult(check=self.name, status=GateStatus.skipped,
                                   metrics={"error": "jscpd não gerou relatório"})

            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                return CheckResult(check=self.name, status=GateStatus.error,
                                   metrics={"error": "relatório jscpd inválido"})

            stats = report.get("statistics", {}).get("total", {})
            percentage = stats.get("percentage", 0.0)
            clones = report.get("duplicates", [])

            changed_set = {f.lstrip("/") for f in changed_files}
            violations = []
            for clone in clones:
                for fragment in clone.get("duplicationA", []) + clone.get("duplicationB", []):
                    file_path = fragment.get("sourceId", "")
                    rel = self._to_rel(file_path, workspace)
                    if rel in changed_set:
                        violations.append(Violation(
                            file=rel,
                            line=fragment.get("start", {}).get("line"),
                            severity="medium",
                            message="Bloco duplicado detectado",
                            current_value=percentage,
                            allowed_value=self.max_percent,
                        ))
                        break

            threshold_exceeded = percentage > self.max_percent
            metrics: dict = {
                "duplication_percent": round(percentage, 2),
                "clone_count": len(clones),
            }
            notes: list[str] = []

            if not threshold_exceeded:
                status = GateStatus.passed
            elif not self.fail_only_on_changed_files or violations:
                status = GateStatus.failed
            else:
                # O teto global estourou, mas nenhum clone caiu nos arquivos
                # alterados. Devolver `passed` deixaria a tabela dizendo 25,85%
                # contra um teto de 5% e mesmo assim "passou", sem explicação.
                status = GateStatus.warning
                notes.append(
                    f"duplicação global {percentage:.2f}% acima do teto "
                    f"({self.max_percent}%), mas nenhum clone nos arquivos alterados "
                    "(fail_only_on_changed_files)"
                )

            if threshold_exceeded:
                clone_paths = [
                    self._to_rel(fragment.get("sourceId", ""), workspace)
                    for clone in clones
                    for fragment in clone.get("duplicationA", [])[:1]
                ]
                if suggestion := _suggest_ignore(clone_paths):
                    notes.append(suggestion)

            if notes:
                metrics["note"] = " · ".join(notes)

            return CheckResult(
                check=self.name,
                status=status,
                metrics=metrics,
                violations=violations,
            )
        finally:
            if owns_report_dir:
                shutil.rmtree(report_dir, ignore_errors=True)

    @staticmethod
    def _to_rel(path: str, workspace: Path) -> str:
        """Caminho relativo ao workspace, sempre com `/`.

        `str(Path)` devolve `tests\\caso.rs` no Windows, e aí o clone nunca casa
        com a lista de arquivos alterados (que vem do git, em posix).
        """
        try:
            return Path(path).relative_to(workspace).as_posix()
        except ValueError:
            return Path(path).as_posix()
