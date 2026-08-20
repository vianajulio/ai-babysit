"""Instância MCP e estado de processo compartilhado pelas tools."""
import json
from pathlib import Path

from app.storage import database
from settings import settings


class _StandaloneMCP:
    """Defers the MCP runtime import until the server is actually started.

    Importing this module is enough for local tool calls and must not pull in
    an HTTP server runtime.  The real FastMCP instance is built only by the
    command-line entry point.
    """

    def __init__(self, name: str):
        self.name = name
        self._tools = []

    def tool(self):
        def register(function):
            self._tools.append(function)
            return function

        return register

    def run(self):
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as exc:  # pragma: no cover - depends on installation
            raise RuntimeError("o pacote mcp é necessário para executar o servidor") from exc

        server = FastMCP(self.name)
        for function in self._tools:
            server.tool()(function)
        server.run()


mcp = _StandaloneMCP("babysit-standalone")

_db_ready = False


def ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    url = settings.babysit_database_url
    if url.startswith("sqlite:///"):
        db_path = Path(url.removeprefix("sqlite:///"))
        if db_path.is_absolute():
            db_path.parent.mkdir(parents=True, exist_ok=True)
    database.init_db()
    _db_ready = True


def exc_err(exc: Exception) -> str:
    return json.dumps({"error": str(exc)})

