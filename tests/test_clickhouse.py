import os

import pytest

pytestmark = [
    pytest.mark.db, pytest.mark.clickhouse,
    pytest.mark.skipif(os.environ.get("CLICKHOUSE_TESTS") != "1", reason="set CLICKHOUSE_TESTS=1"),
]


def _stage_tables(settings) -> int:
    from etl.clickhouse import etl_client
    db = settings.clickhouse().database
    return len(etl_client(settings).query_df(f"SELECT name FROM system.tables WHERE database = '{db}_stage'"))


def test_clickhouse_mart_matches_postgres(dwh, settings):
    from etl.clickhouse import sync_clickhouse
    stats = sync_clickhouse(settings)
    assert stats["cells_ch"] == stats["cells_pg"] > 0
    assert stats["mismatches"] == {}
    # Актуальных строк ровно столько, сколько в рыночной выборке core; промежуточные таблицы удалены.
    assert stats["market_base_rows"] == dwh.execute("SELECT count(*) FROM core.v_market_base").fetchone()[0]
    assert stats["dict_city_rows"] == dwh.execute("SELECT count(*) FROM core.dim_city").fetchone()[0]
    assert _stage_tables(settings) == 0
    # Полная синхронизация заменяет партиции: удаляющих и неслитых версий физически не остаётся.
    from etl.clickhouse import etl_client
    db = settings.clickhouse().database
    physical = etl_client(settings).query_df(f"SELECT count() AS n FROM {db}.market_base")["n"].iloc[0]
    assert physical == stats["market_base_rows"]


def test_clickhouse_window_sync_is_idempotent(dwh, settings):
    """Инкремент за окно поверх полной синхронизации: та же выборка, витрина без расхождений; повтор окна — так же."""
    from datetime import date

    from etl.clickhouse import sync_clickhouse
    from etl.window import Window
    full = sync_clickhouse(settings)
    window = Window(date(2026, 10, 20), date(2026, 10, 26))
    first = sync_clickhouse(settings, window)
    second = sync_clickhouse(settings, window)
    assert full["market_base_rows"] == first["market_base_rows"] == second["market_base_rows"]
    assert first["base_mode"] == second["base_mode"] == "window"
    assert first["months_recomputed"] == second["months_recomputed"] == 0     # витрина PostgreSQL не менялась
    assert second["mismatches"] == {} and second["cells_ch"] == full["cells_ch"]
    assert _stage_tables(settings) == 0


def test_clickhouse_repeats_months_recomputed_in_postgres(dwh, settings, as_of):
    from etl.clickhouse import sync_clickhouse
    from etl.marts import build_marts
    from etl.window import Window
    sync_clickhouse(settings)
    build_marts(settings, as_of)                                   # инкремент: последние N месяцев
    stats = sync_clickhouse(settings, Window(as_of, as_of))
    assert stats["months_recomputed"] == settings.mart_lookback_months
    assert stats["mismatches"] == {}
