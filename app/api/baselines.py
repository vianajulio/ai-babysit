from fastapi import APIRouter, HTTPException

from app.storage import repositories

router = APIRouter(prefix="/baselines", tags=["baselines"])


@router.get("/{repository}")
async def get_baseline(repository: str, branch: str = "main"):
    baseline = repositories.load_baseline(repository, branch)
    if not baseline:
        raise HTTPException(status_code=404, detail="Baseline not found")
    return baseline


@router.post("/{repository}/refresh")
async def refresh_baseline(repository: str, branch: str = "main", metrics: dict = {}):
    repositories.save_baseline(repository, branch, metrics)
    return {"ok": True, "repository": repository, "branch": branch}
