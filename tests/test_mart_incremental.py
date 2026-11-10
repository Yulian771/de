from datetime import date, timedelta

import pytest

from common.db import connect, run_sql_dir, set_params

pytestmark = pytest.mark.db

METRICS = ("p25_trip, median_trip, p75_trip, avg_trip, p25_km, median_km, p75_km, trips_count, carriers_count, "
           "cv_rate, last_price_date, mom_pct, yoy_pct, reliability_level, loading_city, loading_region")
CELLS_SQL = f"""
SELECT md5(string_agg(t::text, '|' ORDER BY month, loading_city_gcid, unloading_city_gcid, body_group, weight_category))
FROM (SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category, {METRICS}
      FROM mart.mart_route_card) t"""


def _build(conn, settings, as_of: date, full: bool) -> int:
    conn.execute("DROP TABLE IF EXISTS mart_months")          # несколько пересчётов в одной транзакции теста
    set_params(conn, {"mart.as_of": as_of, "mart.full": "true" if full else "false",
                      "mart.lookback_months": settings.mart_lookback_months})
    run_sql_dir(conn, settings.sql_dir / "transforms" / "mart")
    return conn.execute("SELECT count(*) FROM mart_months").fetchone()[0]


@pytest.fixture
def tx(dwh, settings):
    with connect(settings.dwh_db) as conn, conn.transaction(force_rollback=True):
        yield conn


def test_incremental_after_as_of_shift_equals_full(tx, settings, as_of):
    """Сдвиг as_of на 3 недели: инкремент пересчитывает последние месяцы и даёт те же метрики и надёжность,
    что полный пересчёт; отличаться может только freshness_days непересчитанных месяцев (на дату их расчёта)."""
    later = as_of + timedelta(days=20)
    recomputed = _build(tx, settings, later, full=False)
    incremental = tx.execute(CELLS_SQL).fetchone()[0]
    assert recomputed == settings.mart_lookback_months
    _build(tx, settings, later, full=True)
    assert tx.execute(CELLS_SQL).fetchone()[0] == incremental


def test_month_computed_before_its_end_is_recomputed(tx, settings, as_of):
    """Месяц вне окна инкремента, посчитанный до своего окончания (например, daily стоял на паузе), пересчитывается."""
    old = date(2025, 1, 1)
    tx.execute("UPDATE mart.mart_route_card SET as_of = %s, trips_count = trips_count + 1000 WHERE month = %s",
               (date(2025, 1, 20), old))
    _build(tx, settings, as_of, full=False)
    assert tx.execute("SELECT count(*) FROM mart.mart_route_card WHERE month = %s AND trips_count > 1000",
                      (old,)).fetchone()[0] == 0
    assert tx.execute("SELECT min(as_of) FROM mart.mart_route_card WHERE month = %s", (old,)).fetchone()[0] == as_of


def test_missing_month_and_its_mom_neighbour_are_recomputed(tx, settings, as_of):
    """Месяц, которого нет в витрине, досчитывается; у следующего месяца пересчитывается MoM."""
    old, nxt = date(2024, 6, 1), date(2024, 7, 1)
    before = tx.execute(CELLS_SQL).fetchone()[0]
    tx.execute("DELETE FROM mart.mart_route_card WHERE month = %s", (old,))
    tx.execute("UPDATE mart.mart_route_card SET mom_pct = NULL WHERE month = %s", (nxt,))
    _build(tx, settings, as_of, full=False)
    assert tx.execute(CELLS_SQL).fetchone()[0] == before


def test_incremental_on_empty_mart_builds_everything(tx, settings, as_of):
    before = tx.execute(CELLS_SQL).fetchone()[0]
    tx.execute("DELETE FROM mart.mart_route_card")
    _build(tx, settings, as_of, full=False)
    assert tx.execute(CELLS_SQL).fetchone()[0] == before
