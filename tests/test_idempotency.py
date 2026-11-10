from datetime import date

import pytest

from etl.extract import run_extract
from etl.marts import build_marts
from etl.transform import run_transform
from etl.window import Window

pytestmark = [pytest.mark.db, pytest.mark.slow]

SNAPSHOT_SQL = {
    "core": "SELECT count(*), sum(price_rub)::numeric(20,2), sum(price_id) FROM core.fact_price",
    "body_groups": "SELECT count(*), sum(price_id) FROM core.fact_price_body_group",
    "months": "SELECT count(*), sum(price_id) FROM core.fact_price_month",
    "quarantine": "SELECT count(*), sum(price_id) FROM dq.dq_quarantine_prices",
    "mart": "SELECT count(*), sum(trips_count), sum(carriers_count), sum(median_trip), sum(p75_km), "
            "sum(coalesce(mom_pct, 0)), sum(coalesce(yoy_pct, 0)), count(*) FILTER (WHERE reliability_level='HIGH') "
            "FROM mart.mart_route_card",
    "mart_hash": "SELECT md5(string_agg(t::text, '|' ORDER BY month, loading_city_gcid, unloading_city_gcid, "
                 "body_group, weight_category)) FROM (SELECT month, loading_city_gcid, unloading_city_gcid, "
                 "body_group, weight_category, trips_count, median_trip, mom_pct, yoy_pct, reliability_level "
                 "FROM mart.mart_route_card) t",
}


def snapshot(conn) -> dict:
    return {name: conn.execute(sql).fetchone() for name, sql in SNAPSHOT_SQL.items()}


def test_rerun_same_window_is_idempotent(dwh, settings, as_of):
    before = snapshot(dwh)
    window = Window(date(2026, 9, 1), date(2026, 9, 30))
    for _ in range(2):
        run_extract(settings, window)
        run_transform(settings, window)
        build_marts(settings, as_of)
    assert snapshot(dwh) == before


def test_transform_rerun_without_extract_is_idempotent(dwh, settings, as_of):
    """Пересборка core из raw без обращения к источнику (например, после правки правил DQ)."""
    before = snapshot(dwh)
    run_transform(settings, Window(date(2026, 10, 1), date(2026, 10, 26)))
    build_marts(settings, as_of)
    assert snapshot(dwh) == before


def test_mart_rebuild_is_idempotent(dwh, settings, as_of):
    """Инкремент и полный пересчёт на ту же дату не меняют витрину."""
    before = snapshot(dwh)
    build_marts(settings, as_of, incremental=True)
    build_marts(settings, as_of)
    build_marts(settings, as_of, incremental=True)
    assert snapshot(dwh) == before
