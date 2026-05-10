from fastapi import APIRouter
from pydantic import BaseModel

from app.providers import azure_devops

router = APIRouter(prefix="/providers/azure", tags=["azure"])


class PrSummary(BaseModel):
    id: int
    title: str
    author: str
    source_branch: str
    target_branch: str
    status: str


class CommentRequest(BaseModel):
    text: str


@router.get("/prs", response_model=list[PrSummary])
async def list_prs():
    prs = await azure_devops.list_pull_requests()
    return [
        PrSummary(
            id=pr["pullRequestId"],
            title=pr["title"],
            author=pr["createdBy"]["displayName"],
            source_branch=pr["sourceRefName"].replace("refs/heads/", ""),
            target_branch=pr["targetRefName"].replace("refs/heads/", ""),
            status=pr["status"],
        )
        for pr in prs
    ]


@router.post("/prs/{pr_id}/gate")
async def run_gate(pr_id: int):
    from app.gates.orchestrator import run_quality_gate
    return await run_quality_gate(pr_id)


@router.post("/prs/{pr_id}/comment")
async def post_comment(pr_id: int, req: CommentRequest):
    await azure_devops.post_comment(pr_id, req.text)
    return {"ok": True}
