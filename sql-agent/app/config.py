"""配置：从项目根目录的 .env 读取。

**两库隔离**（这是本项目的核心安全设计之一）：

    agent_sql  ←  项目自身记录：账本 / 审计。只有应用自己的 postgres 连接能碰。
    cs_v1      ←  智能体执行 SQL 的目标库。执行走受限角色 agent_sql_runner，
                  它 **只有 4 张业务表的 DML 权限，没有任何 DDL 权限**。

所以理论上，即使 sql_guard 被绕过、LLM 生成了 DROP TABLE，
受限角色在数据库层也执行不了 —— 应用层校验能被绕过，权限绕不过去。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

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

    pg_db_ledger: str = "agent_sql"   # 项目自身记录（账本 / 审计）
    pg_db_biz: str = "cs_v1"          # 智能体执行 SQL 的目标库

    # 受限执行角色：只有业务表的 DML 权限
    runner_user: str = "agent_sql_runner"
    runner_password: str = "runner.2024.cs_v1"

    # ---------- DeepSeek ----------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_temperature: float = 0.0
    llm_timeout: float = 90.0
    structured_method: str = "function_calling"

    # ---------- 运行开关 ----------
    llm_mode: str = "deepseek"
    max_rounds: int = 3
    lease_seconds: int = 300
    use_checkpointer: bool = True

    # ---------- 执行沙箱 ----------
    max_rows: int = 500
    statement_timeout_ms: int = 5000
    confirm_row_threshold: int = 1000

    # ---------------------------------------------------------------- DSN

    def _dsn(self, db: str, user: str | None = None, password: str | None = None) -> str:
        return (f"postgresql://{user or self.pg_user}:{password or self.pg_password}"
                f"@{self.pg_host}:{self.pg_port}/{db}")

    @property
    def ledger_dsn(self) -> str:
        """项目自身记录：agent_sql。"""
        return self._dsn(self.pg_db_ledger)

    @property
    def biz_admin_dsn(self) -> str:
        """业务库的管理连接：建表、灌种子数据。"""
        return self._dsn(self.pg_db_biz)

    @property
    def biz_runner_dsn(self) -> str:
        """业务库的受限连接：**只**用它跑用户 SQL 和 EXPLAIN。"""
        return self._dsn(self.pg_db_biz, self.runner_user, self.runner_password)

    @property
    def runner_ledger_dsn(self) -> str:
        """受限角色连到项目记录库 —— 用来实测它读不到系统表。"""
        return self._dsn(self.pg_db_ledger, self.runner_user, self.runner_password)

    @property
    def maintenance_dsn(self) -> str:
        """维护库：建角色用（角色是集群级的，不属于某个库）。"""
        return self._dsn("postgres")

    # ---------------------------------------------------------------- 路径

    @property
    def project_root(self) -> str:
        return str(BASE_DIR)

    @property
    def ledger_schema_path(self) -> Path:
        return BASE_DIR / "sql" / "ledger.sql"

    @property
    def biz_schema_path(self) -> Path:
        return BASE_DIR / "sql" / "biz.sql"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
