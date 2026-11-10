"""Конфигурация из переменных окружения (см. .env.example)"""

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from psycopg.conninfo import make_conninfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "ul-platform"
DEFAULT_USER = "ul_etl"


def _env(name: str, default: str | None = None) -> str:
    """Значение переменной окружения"""
    value = os.environ.get(name) or default
    if value is None:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    connect_timeout_s: int = 10
    statement_timeout_s: int = 1800

    @property
    def conninfo(self) -> str:
        """Строка подключения libpq (make_conninfo экранирует пробелы, кавычки)."""
        return make_conninfo(
            host=self.host, port=self.port, dbname=self.dbname, user=self.user, password=self.password,
            application_name=APP_NAME, connect_timeout=self.connect_timeout_s,
            options=f"-c statement_timeout={self.statement_timeout_s * 1000}",
        )

    @classmethod
    def from_env(cls, prefix: str, default_db: str) -> "DbConfig":
        return cls(
            host=_env(f"{prefix}_HOST", "localhost"),
            port=int(_env(f"{prefix}_PORT", "5432")),
            dbname=_env(f"{prefix}_NAME", default_db),
            user=_env(f"{prefix}_USER", DEFAULT_USER),
            password=_env(f"{prefix}_PASSWORD"),
            connect_timeout_s=int(_env("DB_CONNECT_TIMEOUT_S", "10")),
            statement_timeout_s=int(_env("DB_STATEMENT_TIMEOUT_S", "1800")),
        )


@dataclass(frozen=True)
class ClickHouseConfig:
    host: str
    port: int
    # Техническая учётная запись: вся работа пайплайна (загрузка, витрина, сверка).
    user: str
    password: str = field(repr=False)
    database: str = "ul_platform"
    # Администратор: только для DDL, создание ETL-пользователя и прав.
    admin_user: str = "default"
    admin_password: str = field(default="", repr=False)


@dataclass(frozen=True)
class Settings:
    source_db: DbConfig
    dwh_db: DbConfig
    # Дата снимка для ручных запусков CLI (необязательно). Порядок выбора — etl.window.resolve_as_of.
    as_of: date | None
    model_dir: Path
    random_seed: int
    log_level: str = "INFO"
    # Инкрементальный пересчёт витрины: последние N месяцев до as_of. N >= 2: ячейки, которые могут быть HIGH
    # (последняя сделка не старше 30 дней от as_of), всегда в двух последних месяцах.
    mart_lookback_months: int = 3
    sql_dir: Path = field(default=PROJECT_ROOT / "sql")

    def __post_init__(self) -> None:
        if self.mart_lookback_months < 2:
            raise ValueError(f"MART_LOOKBACK_MONTHS must be >= 2, got {self.mart_lookback_months}")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source_db=DbConfig.from_env("SOURCE_DB", "deals_bigdata"),
            dwh_db=DbConfig.from_env("DWH_DB", "dwh"),
            as_of=date.fromisoformat(os.environ["AS_OF"]) if os.environ.get("AS_OF") else None,
            model_dir=Path(_env("MODEL_DIR", str(PROJECT_ROOT / "artifacts"))),
            random_seed=int(_env("RANDOM_SEED", "42")),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            mart_lookback_months=int(_env("MART_LOOKBACK_MONTHS", "3")),
        )

    def clickhouse(self) -> ClickHouseConfig:
        """Настройки ClickHouse"""
        return ClickHouseConfig(
            host=_env("CLICKHOUSE_HOST", "localhost"),
            port=int(_env("CLICKHOUSE_PORT", "8123")),
            user=_env("CLICKHOUSE_USER", DEFAULT_USER),
            password=_env("CLICKHOUSE_PASSWORD"),
            database=_env("CLICKHOUSE_DATABASE", "ul_platform"),
            admin_user=_env("CLICKHOUSE_ADMIN_USER", "default"),
            admin_password=os.environ.get("CLICKHOUSE_ADMIN_PASSWORD", ""),   # для clickhouse-init
        )
