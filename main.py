from fastapi import FastAPI

from app.api import azure, baselines, gate_runs, health, local, review
from app.storage.database import init_db

app = FastAPI(title="babysit", description="Quality Gate para Pull Requests")

app.include_router(health.router)
app.include_router(azure.router)
app.include_router(local.router)
app.include_router(review.router)
app.include_router(gate_runs.router)
app.include_router(baselines.router)


@app.on_event("startup")
async def startup():
    init_db()
