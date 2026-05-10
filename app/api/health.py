from fastapi import APIRouter

from settings import settings

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "model": settings.ollama_model}
