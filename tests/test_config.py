from datetime import date
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from common.config import APP_NAME, ClickHouseConfig, DbConfig, Settings

ENV_PREFIXES = ("SOURCE_DB", "DWH_DB")


def _cfg(password: str = "p@ss word';x\\") -> DbConfig:
    return DbConfig(host="db", port=5432, dbname="dwh", user="ul_etl", password=password,
                    connect_timeout_s=7, statement_timeout_s=60)


def test_conninfo_quotes_special_characters():
    parsed = conninfo_to_dict(_cfg().conninfo)
    assert parsed["password"] == "p@ss word';x\\"
    assert parsed["application_name"] == APP_NAME
    assert parsed["connect_timeout"] == "7"
    assert parsed["options"] == "-c statement_timeout=60000"


def test_secrets_not_in_repr():
    secret = "very-secret-value"
    assert secret not in repr(_cfg(secret))
    ch = ClickHouseConfig(host="ch", port=8123, user="ul_etl", password=secret, admin_password=secret)
    assert secret not in repr(ch)
    settings = Settings(source_db=_cfg(secret), dwh_db=_cfg(secret), as_of=date(2026, 10, 26),
                        model_dir=Path("/tmp"), random_seed=42)
    assert secret not in repr(settings)


@pytest.fixture
def clean_env(monkeypatch):
    for prefix in ENV_PREFIXES:
        for suffix in ("HOST", "PORT", "NAME", "USER", "PASSWORD"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    for name in ("LOG_LEVEL", "DB_CONNECT_TIMEOUT_S", "DB_STATEMENT_TIMEOUT_S", "CLICKHOUSE_PASSWORD", "AS_OF"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_password_is_required(clean_env):
    with pytest.raises(RuntimeError, match="SOURCE_DB_PASSWORD"):
        Settings.from_env()


def test_clickhouse_password_is_required(clean_env):
    clean_env.setenv("SOURCE_DB_PASSWORD", "x")
    clean_env.setenv("DWH_DB_PASSWORD", "x")
    with pytest.raises(RuntimeError, match="CLICKHOUSE_PASSWORD"):
        Settings.from_env().clickhouse()


def test_empty_password_is_rejected(clean_env):
    clean_env.setenv("SOURCE_DB_PASSWORD", "")
    clean_env.setenv("DWH_DB_PASSWORD", "x")
    with pytest.raises(RuntimeError, match="SOURCE_DB_PASSWORD"):
        Settings.from_env()


def test_defaults_and_log_level(clean_env):
    clean_env.setenv("SOURCE_DB_PASSWORD", "a")
    clean_env.setenv("DWH_DB_PASSWORD", "b")
    clean_env.setenv("LOG_LEVEL", "debug")
    s = Settings.from_env()
    assert s.log_level == "DEBUG"
    assert (s.source_db.user, s.source_db.dbname, s.dwh_db.dbname) == ("ul_etl", "deals_bigdata", "dwh")
    assert s.dwh_db.statement_timeout_s == 1800
    assert s.as_of is None                      # необязательна: по умолчанию — последний день данных
    clean_env.setenv("AS_OF", "2026-10-26")
    assert Settings.from_env().as_of == date(2026, 10, 26)


def test_mart_lookback_must_cover_two_months(clean_env):
    clean_env.setenv("SOURCE_DB_PASSWORD", "a")
    clean_env.setenv("DWH_DB_PASSWORD", "b")
    clean_env.setenv("MART_LOOKBACK_MONTHS", "1")
    with pytest.raises(ValueError, match="MART_LOOKBACK_MONTHS"):
        Settings.from_env()
