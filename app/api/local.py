from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.gates.orchestrator import run_local_quality_gate

router = APIRouter(prefix="/local", tags=["local"])


class LocalGateRequest(BaseModel):
    workspace: str
    files: list[str] = Field(min_length=1)
    repository: str = "local"
    branch: str = "local"
    use_ratchet: bool = False


def _resolve_changed_files(workspace: Path, files: list[str]) -> list[str]:
    if not workspace.exists() or not workspace.is_dir():
        raise HTTPException(status_code=400, detail="Workspace local não encontrado")

    root = workspace.resolve()
    changed_files: list[str] = []

    for file_name in files:
        candidate = Path(file_name)
        full_path = candidate if candidate.is_absolute() else root / candidate

        try:
            resolved = full_path.resolve(strict=True)
            rel_path = resolved.relative_to(root)
        except FileNotFoundError:
            raise HTTPException(status_code=400, detail=f"Arquivo não encontrado: {file_name}")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Arquivo fora do workspace: {file_name}")

        if not resolved.is_file():
            raise HTTPException(status_code=400, detail=f"Não é um arquivo: {file_name}")

        changed_files.append(rel_path.as_posix())

    return changed_files


@router.post("/gate")
async def run_local_gate(req: LocalGateRequest):
    workspace = Path(req.workspace)
    changed_files = _resolve_changed_files(workspace, req.files)
    return await run_local_quality_gate(
        workspace.resolve(),
        changed_files,
        repository=req.repository,
        branch=req.branch,
        use_ratchet=req.use_ratchet,
    )
