import json

import httpx
from fastapi import HTTPException

from settings import settings


async def generate(prompt: str, expect_json: bool = False) -> str:
    payload: dict = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
    }
    if expect_json:
        payload["format"] = "json"

    try:
        async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
            resp = await client.post(settings.ollama_url, json=payload)
            resp.raise_for_status()
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail=f"Ollama não está rodando em {settings.ollama_url}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Ollama erro: {e.response.status_code}")

    return resp.json().get("response", "")


async def generate_json(prompt: str) -> dict:
    raw = await generate(prompt, expect_json=True)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail=f"Ollama retornou JSON inválido: {raw[:500]}")
