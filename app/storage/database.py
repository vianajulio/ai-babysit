from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Session

from settings import settings


def create_sqlite_engine(url: str):
    """Engine com WAL ligado quando a URL é SQLite.

    Vários subagentes gravam resultado parcial em paralelo (`run_review_task`,
    `submit_task_findings`). No journal padrão, escritas concorrentes viram
    `database is locked`; com WAL e `busy_timeout`, elas apenas esperam a vez —
    as transações aqui são curtas e raras (uma por task).
    """
    new_engine = create_engine(url, connect_args={"check_same_thread": False})
    if not url.startswith("sqlite"):
        return new_engine

    @event.listens_for(new_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # pragma: no cover - trivial
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    return new_engine


engine = create_sqlite_engine(settings.babysit_database_url)


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
