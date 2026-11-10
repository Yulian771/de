import pytest

from common.config import Settings
from common.db import connect


@pytest.fixture(scope="session")
def settings() -> Settings:
    try:
        return Settings.from_env()
    except RuntimeError as exc:
        pytest.skip(f"DWH settings are not configured: {exc}")


@pytest.fixture(scope="session")
def dwh(settings):
    try:
        conn = connect(settings.dwh_db, autocommit=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DWH is not reachable: {exc}")
    loaded = conn.execute("SELECT to_regclass('core.fact_price') IS NOT NULL").fetchone()[0]
    if not loaded or conn.execute("SELECT count(*) FROM core.fact_price").fetchone()[0] == 0:
        pytest.skip("DWH is empty: run `python -m pipeline run-all` first")
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def as_of(dwh):
    """Дата снимка, на которую построена витрина (её же используют ML и ClickHouse)."""
    value = dwh.execute("SELECT max(as_of) FROM mart.mart_route_card").fetchone()[0]
    if value is None:
        pytest.skip("mart is empty: run `python -m pipeline run-all` first")
    return value


@pytest.fixture(scope="session")
def source(settings):
    conn = connect(settings.source_db, autocommit=True)
    yield conn
    conn.close()


def scalar(conn, sql: str, params=None):
    return conn.execute(sql, params).fetchone()[0]
