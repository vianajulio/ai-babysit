from fastapi import APIRouter, HTTPException

from app.storage import repositories

router = APIRouter(prefix="/gate-runs", tags=["gate-runs"])


@router.get("/{run_id}")
async def get_run(run_id: str):
    run = repositories.get_gate_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/{run_id}/summary")
async def get_summary(run_id: str):
    run = repositories.get_gate_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    failed = [c for c in run.get("checks", []) if c["status"] == "failed"]
    return {
        "run_id": run_id,
        "status": run["status"],
        "total_checks": len(run.get("checks", [])),
        "failed_checks": len(failed),
        "failed": [c["check"] for c in failed],
    }
