from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    return Session(engine)
