from pathlib import Path
from typing import Any

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    azure_pat: str = ""
    azure_org: str = ""
    azure_project: str = ""
    azure_repo: str = ""
    azure_api_version: str = "7.1"

    ollama_url: str = "http://localhost:11434/api/generate"
    ollama_model: str = "qwen2.5-coder:7b"
    ollama_timeout: float = 120.0

    babysit_temp_dir: str = "/tmp/babysit"
    babysit_database_url: str = "sqlite:///./babysit.db"
    babysit_workspace_timeout: int = 300
    babysit_cleanup_after_run: bool = True

    standards_path: str = "/mnt/jogos/Plus/energia/backend/docs/coding-standards.md"

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


def _load_project_config(workspace: Path) -> dict:
    for file_name in (".babysit.yml", ".babysit.yaml"):
        cfg_path = workspace / file_name
        if cfg_path.exists():
            with cfg_path.open() as f:
                return yaml.safe_load(f) or {}
    return {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


settings = Settings()
