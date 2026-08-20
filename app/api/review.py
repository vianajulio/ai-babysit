from fastapi import APIRouter
from pydantic import BaseModel

from app.ai import ollama
from app.ai.prompts import build_review_prompt, load_standards

router = APIRouter(tags=["review"])


class ReviewRequest(BaseModel):
    language: str
    filePath: str
    code: str
    diff: str | None = None
    reviewMode: str = "strict"


@router.post("/review")
async def review_file(req: ReviewRequest):
    standards = load_standards()
    prompt = build_review_prompt(
        standards=standards,
        language=req.language,
        file_path=req.filePath,
        code=req.code,
        diff=req.diff,
        review_mode=req.reviewMode,
    )
    return await ollama.generate_json(prompt)
