"""
MCP server standalone — importa o orchestrator diretamente, sem precisar
que um servidor HTTP esteja rodando.

Este arquivo é só a fachada: as tools moram em `app/mcp/` (agrupadas por
assunto) e se registram na instância compartilhada ao serem importadas. Os
nomes reexportados aqui mantêm o import histórico
`import mcp_server_standalone as server` funcionando.
"""
import sys
from pathlib import Path

# Garante que o projeto esteja no sys.path independente de onde o servidor é iniciado
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.mcp.agent_review_tools import (  # noqa: E402
    get_task_context,
    submit_task_findings,
)
from app.mcp.gate_tools import (  # noqa: E402
    get_gate_run,
    get_gate_run_summary,
    review_file,
    run_commit_gate,
    run_local_gate,
)
from app.mcp.review_tools import (  # noqa: E402
    close_review_plan,
    get_review_plan,
    plan_pr_review,
    run_review_task,
)
from app.mcp.runtime import mcp  # noqa: E402

__all__ = [
    "close_review_plan",
    "get_gate_run",
    "get_gate_run_summary",
    "get_review_plan",
    "get_task_context",
    "mcp",
    "plan_pr_review",
    "review_file",
    "run_commit_gate",
    "run_local_gate",
    "run_review_task",
    "submit_task_findings",
]


if __name__ == "__main__":
    mcp.run()
