"""Arquivo alterado com o peso usado para balancear as fatias."""
from pydantic import BaseModel


class ChangedFile(BaseModel):
    path: str
    added_lines: int = 0
