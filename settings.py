from pathlib import Path
from typing import Any

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    azure_pat: str = ""
    azure_org: str = ""
    azure_project: str = ""
    azure_repo: str = ""
    azure_api_version: str = "7.1"

    ollama_url: str = "http://localhost:11434/api/generate"
    ollama_model: str = "qwen2.5-coder:7b"
    ollama_timeout: float = 120.0

    # Relative defaults keep local/MCP runs portable.  Callers that need a
    # shared location can still provide an explicit value through the env.
    babysit_temp_dir: str = ".babysit/workspaces"
    babysit_database_url: str = "sqlite:///./babysit.db"
    babysit_workspace_timeout: int = 300
    babysit_cleanup_after_run: bool = True

    standards_path: str = "docs/coding-standards.md"

    @field_validator("azure_pat", "azure_org", "azure_project", "azure_repo", mode="before")
    @classmethod
    def strip_value(cls, v: str) -> str:
        return v.strip() if v else v

    @property
    def quality_gate_config(self) -> dict:
        cfg_path = Path(__file__).parent / "quality_gate.yaml"
        if not cfg_path.exists():
            return {}
        with cfg_path.open() as f:
            return yaml.safe_load(f) or {}

    def load_quality_gate_config(self, workspace: Path | None = None) -> dict:
        config = self.quality_gate_config
        if workspace is None:
            return config

        project_config = _load_project_config(workspace)
        if not project_config:
            return config

        return _deep_merge(config, project_config)


def project_config_path(workspace: Path) -> Path | None:
    """Arquivo de config do projeto revisado, ou `None` se ele não tiver um.

    Sem isso o gate roda com os defaults globais do Babysit sem contar a
    ninguém, e o leitor do relatório não sabe se um limite é regra do projeto
    ou palpite da ferramenta.
    """
    for file_name in (".babysit.yml", ".babysit.yaml"):
        cfg_path = workspace / file_name
        if cfg_path.exists():
            return cfg_path
    return None


def _load_project_config(workspace: Path) -> dict:
    cfg_path = project_config_path(workspace)
    if cfg_path is None:
        return {}
    with cfg_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        base_value = merged.get(key)
        if isinstance(value, dict) and isinstance(base_value, dict):
            merged[key] = _deep_merge(base_value, value)
        elif isinstance(value, list) and isinstance(base_value, list):
            merged[key] = _merge_lists(base_value, value)
        else:
            merged[key] = value
    return merged


def _merge_lists(base: list, override: list) -> list:
    merged = list(base)
    for item in override:
        if item not in merged:
            merged.append(item)
    return merged


settings = Settings()
