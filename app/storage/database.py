from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from settings import settings

engine = create_engine(settings.babysit_database_url, connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


class GateRunRecord(Base):
    __tablename__ = "gate_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), unique=True, nullable=False, index=True)
    pr_id = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False)
    payload_json = Column(Text, nullable=False)


class BaselineRecord(Base):
    __tablename__ = "baselines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repository = Column(String(256), nullable=False)
    branch = Column(String(256), nullable=False)
    metrics_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class ReviewPlanRecord(Base):
    __tablename__ = "review_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(String(64), unique=True, nullable=False, index=True)
    workspace = Column(String(1024), nullable=False)
    repository = Column(String(256), nullable=False)
    branch = Column(String(256), nullable=False)
    status = Column(String(16), nullable=False)      # pending | complete
    created_at = Column(DateTime, nullable=False)
    payload_json = Column(Text, nullable=False)      # plano completo devolvido ao cliente


class ReviewTaskRecord(Base):
    __tablename__ = "review_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(String(64), nullable=False, index=True)
    task_id = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)      # pending | done | error
    result_json = Column(Text, nullable=True)        # list[CheckResult]
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint("plan_id", "task_id", name="uq_review_task"),)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    return Session(engine)
