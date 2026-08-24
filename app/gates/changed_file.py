"""Arquivo alterado com o peso e a origem usados pelo gate."""
from pydantic import BaseModel


class ChangedFile(BaseModel):
    path: str
    added_lines: int = 0
    # Status do git para o arquivo no diff: "A" (criado por esta mudança),
    # "M", "R"... Decide se uma violação reprova ou apenas avisa.
    status: str = "M"
