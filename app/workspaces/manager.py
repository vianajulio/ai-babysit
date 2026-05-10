import asyncio
import shutil
import uuid
from pathlib import Path

from settings import settings


def _workspace_root() -> Path:
    return Path(settings.babysit_temp_dir)


def create_workspace(run_id: str | None = None) -> Path:
    run_id = run_id or str(uuid.uuid4())
    workspace = _workspace_root() / run_id / "repo"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


async def clone_repository(workspace: Path, clone_url: str) -> None:
    parent = workspace.parent
    cmd = ["git", "clone", "--depth", "1", clone_url, str(workspace)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.babysit_workspace_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"git clone excedeu timeout de {settings.babysit_workspace_timeout}s")

    if proc.returncode != 0:
        # Never surface the clone URL (may contain PAT)
        raise RuntimeError(f"git clone falhou: {stderr.decode(errors='replace')[-500:]}")


async def checkout_branch(workspace: Path, branch: str) -> None:
    for cmd in [
        ["git", "fetch", "origin", branch],
        ["git", "checkout", "-B", branch, "FETCH_HEAD"],
    ]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"git {cmd[1]} excedeu timeout")
        if proc.returncode != 0:
            raise RuntimeError(f"git {cmd[1]} falhou: {stderr.decode(errors='replace')[-300:]}")


def cleanup_workspace(workspace: Path) -> None:
    run_dir = workspace.parent
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)


async def prepare_workspace(run_id: str, clone_url: str, branch: str) -> Path:
    workspace = create_workspace(run_id)
    try:
        await clone_repository(workspace, clone_url)
        await checkout_branch(workspace, branch)
    except Exception:
        cleanup_workspace(workspace)
        raise
    return workspace
