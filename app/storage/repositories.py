import json
from datetime import datetime

from sqlalchemy import select

from app.gates.models import GateRun
from app.storage.database import (
    BaselineRecord,
    GateRunRecord,
    ReviewPlanRecord,
    ReviewTaskRecord,
    get_session,
)


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


def save_review_plan(
    plan_id: str,
    workspace: str,
    repository: str,
    branch: str,
    status: str,
    payload: dict,
) -> None:
    with get_session() as session:
        existing = session.scalar(
            select(ReviewPlanRecord).where(ReviewPlanRecord.plan_id == plan_id)
        )
        if existing:
            existing.workspace = workspace
            existing.repository = repository
            existing.branch = branch
            existing.status = status
            existing.payload_json = json.dumps(payload)
        else:
            record = ReviewPlanRecord(
                plan_id=plan_id,
                workspace=workspace,
                repository=repository,
                branch=branch,
                status=status,
                created_at=datetime.utcnow(),
                payload_json=json.dumps(payload),
            )
            session.add(record)
        session.commit()


def load_review_plan(plan_id: str) -> dict | None:
    with get_session() as session:
        record = session.scalar(
            select(ReviewPlanRecord).where(ReviewPlanRecord.plan_id == plan_id)
        )
        if not record:
            return None
        return {
            "plan_id": record.plan_id,
            "workspace": record.workspace,
            "repository": record.repository,
            "branch": record.branch,
            "status": record.status,
            "created_at": record.created_at,
            "payload": json.loads(record.payload_json),
        }


def save_review_task_result(
    plan_id: str,
    task_id: str,
    status: str,
    result: list[dict] | None = None,
) -> None:
    with get_session() as session:
        existing = session.scalar(
            select(ReviewTaskRecord).where(
                ReviewTaskRecord.plan_id == plan_id,
                ReviewTaskRecord.task_id == task_id,
            )
        )
        result_json = json.dumps(result) if result is not None else None
        if existing:
            existing.status = status
            existing.result_json = result_json
            existing.updated_at = datetime.utcnow()
        else:
            record = ReviewTaskRecord(
                plan_id=plan_id,
                task_id=task_id,
                status=status,
                result_json=result_json,
                updated_at=datetime.utcnow(),
            )
            session.add(record)
        session.commit()


def load_review_tasks(plan_id: str) -> list[dict]:
    with get_session() as session:
        records = session.scalars(
            select(ReviewTaskRecord).where(ReviewTaskRecord.plan_id == plan_id)
        ).all()
        return [
            {
                "plan_id": record.plan_id,
                "task_id": record.task_id,
                "status": record.status,
                "result": json.loads(record.result_json) if record.result_json else None,
                "updated_at": record.updated_at,
            }
            for record in records
        ]


def mark_plan_complete(plan_id: str) -> None:
    with get_session() as session:
        record = session.scalar(
            select(ReviewPlanRecord).where(ReviewPlanRecord.plan_id == plan_id)
        )
        if record:
            record.status = "complete"
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
