import json
from datetime import datetime

from sqlalchemy import select

from app.gates.models import GateRun
from app.storage.database import BaselineRecord, GateRunRecord, get_session


def save_gate_run(run: GateRun) -> None:
    with get_session() as session:
        existing = session.scalar(select(GateRunRecord).where(GateRunRecord.run_id == run.run_id))
        if existing:
            existing.status = run.status.value
            existing.payload_json = run.model_dump_json()
        else:
            record = GateRunRecord(
                run_id=run.run_id,
                pr_id=run.pr_id,
                status=run.status.value,
                created_at=run.created_at,
                payload_json=run.model_dump_json(),
            )
            session.add(record)
        session.commit()


def get_gate_run(run_id: str) -> dict | None:
    with get_session() as session:
        record = session.scalar(select(GateRunRecord).where(GateRunRecord.run_id == run_id))
        if not record:
            return None
        return json.loads(record.payload_json)


def save_baseline(repository: str, branch: str, metrics: dict) -> None:
    with get_session() as session:
        existing = session.scalar(
            select(BaselineRecord).where(
                BaselineRecord.repository == repository,
                BaselineRecord.branch == branch,
            )
        )
        if existing:
            existing.metrics_json = json.dumps(metrics)
            existing.updated_at = datetime.utcnow()
        else:
            record = BaselineRecord(
                repository=repository,
                branch=branch,
                metrics_json=json.dumps(metrics),
                updated_at=datetime.utcnow(),
            )
            session.add(record)
        session.commit()


def load_baseline(repository: str, branch: str) -> dict | None:
    with get_session() as session:
        record = session.scalar(
            select(BaselineRecord).where(
                BaselineRecord.repository == repository,
                BaselineRecord.branch == branch,
            )
        )
        if not record:
            return None
        return json.loads(record.metrics_json)
