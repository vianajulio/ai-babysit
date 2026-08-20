from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class GateStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    warning = "warning"
    skipped = "skipped"
    error = "error"


class Violation(BaseModel):
    file: str = ""
    line: int | None = None
    severity: str = "medium"
    message: str = ""
    current_value: float | None = None
    allowed_value: float | None = None
    # Preenchidos por findings de subagente (`agent_review`); runners
    # determinísticos deixam vazio.
    category: str = ""
    suggestion: str = ""


class CheckResult(BaseModel):
    check: str
    status: GateStatus
    language: str = "any"
    metrics: dict[str, Any] = Field(default_factory=dict)
    violations: list[Violation] = Field(default_factory=list)
    suggestion: str = ""


class GateRun(BaseModel):
    run_id: str
    pr_id: int
    repository: str = ""
    source_branch: str = ""
    target_branch: str = ""
    status: GateStatus
    checks: list[CheckResult] = Field(default_factory=list)
    comment_posted: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
