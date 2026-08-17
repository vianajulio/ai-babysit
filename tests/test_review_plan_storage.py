import pytest

from app.storage import database, repositories


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isola cada teste em um SQLite próprio, sem tocar em babysit.db."""
    from sqlalchemy import create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    return engine


def test_task_result_is_saved_and_plan_reports_pending_tasks(db):
    payload = {
        "plan_id": "plan-1",
        "mode": "sharded",
        "tasks": [{"task_id": "global"}, {"task_id": "files-1"}],
    }
    repositories.save_review_plan(
        plan_id="plan-1",
        workspace="/repo",
        repository="repo",
        branch="main",
        status="pending",
        payload=payload,
    )

    repositories.save_review_task_result(
        plan_id="plan-1",
        task_id="global",
        status="done",
        result=[{"check": "duplication", "status": "passed"}],
    )

    plan = repositories.load_review_plan("plan-1")
    tasks = repositories.load_review_tasks("plan-1")

    assert plan is not None
    assert plan["status"] == "pending"

    done_ids = {t["task_id"] for t in tasks if t["status"] == "done"}
    pending_ids = [t["task_id"] for t in plan["payload"]["tasks"] if t["task_id"] not in done_ids]

    assert done_ids == {"global"}
    assert pending_ids == ["files-1"]


def test_saving_the_same_task_twice_overwrites_instead_of_duplicating(db):
    repositories.save_review_plan(
        plan_id="plan-2",
        workspace="/repo",
        repository="repo",
        branch="main",
        status="pending",
        payload={"tasks": [{"task_id": "files-1"}]},
    )

    repositories.save_review_task_result(
        plan_id="plan-2",
        task_id="files-1",
        status="done",
        result=[{"check": "file_size", "status": "failed"}],
    )
    repositories.save_review_task_result(
        plan_id="plan-2",
        task_id="files-1",
        status="done",
        result=[{"check": "file_size", "status": "passed"}],
    )

    tasks = repositories.load_review_tasks("plan-2")

    assert len(tasks) == 1
    assert tasks[0]["result"][0]["status"] == "passed"


def test_plan_is_complete_only_when_every_task_has_a_result(db):
    payload = {"tasks": [{"task_id": "a"}, {"task_id": "b"}]}
    repositories.save_review_plan(
        plan_id="plan-3",
        workspace="/repo",
        repository="repo",
        branch="main",
        status="pending",
        payload=payload,
    )

    repositories.save_review_task_result(plan_id="plan-3", task_id="a", status="done", result=[])

    tasks = repositories.load_review_tasks("plan-3")
    task_ids = {t["task_id"] for t in payload["tasks"]}
    done_ids = {t["task_id"] for t in tasks if t["status"] == "done"}
    assert done_ids != task_ids

    plan = repositories.load_review_plan("plan-3")
    assert plan["status"] == "pending"

    repositories.save_review_task_result(plan_id="plan-3", task_id="b", status="done", result=[])

    tasks = repositories.load_review_tasks("plan-3")
    done_ids = {t["task_id"] for t in tasks if t["status"] == "done"}
    assert done_ids == task_ids

    repositories.mark_plan_complete("plan-3")
    plan = repositories.load_review_plan("plan-3")
    assert plan["status"] == "complete"
