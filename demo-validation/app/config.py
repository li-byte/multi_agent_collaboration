"""配置：从项目根目录的 .env 读取，产出 DSN 与运行开关。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config.py -> 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- PostgreSQL ----------
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_db: str = "agents"

    # ---------- DeepSeek ----------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_temperature: float = 0.0
    llm_timeout: float = 90.0
    # function_calling / json_mode / auto —— DeepSeek 不支持 json_schema
    structured_method: str = "function_calling"

    # ---------- 运行开关 ----------
    llm_mode: str = "deepseek"      # deepseek | mock
    max_rounds: int = 3
    lease_seconds: int = 300
    use_checkpointer: bool = True   # off 时排障用：不挂 Postgres checkpoint

    @property
    def dsn(self) -> str:
        """psycopg3 / LangGraph 通用的连接串。"""
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )

    @property
    def project_root(self) -> str:
        return str(BASE_DIR)

    @property
    def schema_path(self) -> Path:
        return BASE_DIR / "schema.sql"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
