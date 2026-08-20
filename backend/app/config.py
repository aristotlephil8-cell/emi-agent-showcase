from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    """Process-only configuration. Secrets are never serialized into graph state."""

    model_config = SettingsConfigDict(
        env_prefix="EMI_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    checkpoint_db: Path = BACKEND_ROOT / ".runtime" / "emi_agent.sqlite"
    cases_file: Path = BACKEND_ROOT / "data" / "dev_cases.jsonl"
    fixture_file: Path = BACKEND_ROOT / "data" / "fixture_responses.json"
    evaluation_summary_file: Path = PROJECT_ROOT / "evaluation" / "results" / "summary.json"

    dashscope_api_key: str | None = Field(default=None, alias="DASHSCOPE_API_KEY")
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="DASHSCOPE_BASE_URL",
    )
    dashscope_model: str = "qwen3.7-plus-2026-05-26"
    model_max_tokens: int = Field(default=2048, ge=256, le=32768)
    request_timeout_seconds: float = 60.0
    fixture_delay_seconds: float = 0.0

    @property
    def live_provider_available(self) -> bool:
        return bool(self.dashscope_api_key)
