from datetime import date

import pytest

from tests.conftest import scalar

pytestmark = pytest.mark.db

# Независимая реализация правил (без core и без справочника групп): только источник deals_bigdata.
REFERENCE_SQL = """
WITH city AS (SELECT DISTINCT gc_id FROM cities),
p AS (
    SELECT p.*,
           CASE WHEN p.currency_code = '643' THEN p.price ELSE p.price * e.value END AS price_rub
    FROM prices p
    LEFT JOIN exchange_rates e ON e.num_code = p.currency_code AND e.setting_date = p.nearest_rate_date
    WHERE p.is_fact AND NOT p.complex AND NOT p.outlier_mark
      AND p.price > 0 AND p.distance > 0 AND p.total_weight >= 0 AND p.total_volume >= 0
      AND ((p.currency_code = '643' AND p.price > 1000) OR (p.currency_code IN ('840','978') AND e.value IS NOT NULL))
      AND p.loading_city_gcid IN (SELECT gc_id FROM city) AND p.unloading_city_gcid IN (SELECT gc_id FROM city)
),
codes AS (
    SELECT p.id, b.body_type_id AS code FROM p JOIN price_body_types b ON b.price_id = p.id
    UNION ALL
    SELECT p.id, p.body_type FROM p
    WHERE NOT EXISTS (SELECT 1 FROM price_body_types b WHERE b.price_id = p.id)
),
grp AS (
    SELECT DISTINCT id,
           CASE WHEN code IS NULL THEN 'Не указан'
                WHEN code IN (56, 57, 58, 59, 60, 61) THEN 'Тент'
                WHEN code IN (4, 46, 47, 48) THEN 'Рефрижератор'
                WHEN code = 2 THEN 'Изотерм'
                WHEN code IN (1, 15) THEN 'Борт'
                ELSE 'Прочие' END AS body_group
    FROM codes
),
mon AS (
    SELECT DISTINCT price_id FROM price_dates
    WHERE date_trunc('month', price_date) = %(month)s AND price_date <= %(as_of)s
)
SELECT p.loading_city_gcid, p.unloading_city_gcid, g.body_group, p.weight_category,
       count(*) AS trips, percentile_cont(0.5) WITHIN GROUP (ORDER BY p.price_rub) AS median
FROM p JOIN grp g ON g.id = p.id JOIN mon m ON m.price_id = p.id
GROUP BY 1, 2, 3, 4
"""


@pytest.mark.parametrize("month", [date(2024, 12, 1), date(2026, 9, 1), date(2026, 10, 1)])
def test_mart_matches_independent_reference(dwh, source, as_of, month):
    ref = {r[:4]: (r[4], r[5]) for r in source.execute(REFERENCE_SQL, {"month": month, "as_of": as_of})}
    mart = {r[:4]: (r[4], float(r[5])) for r in dwh.execute(
        "SELECT loading_city_gcid, unloading_city_gcid, body_group, weight_category, trips_count, median_trip "
        "FROM mart.mart_route_card WHERE month = %s", (month,))}
    assert ref, "reference is empty"
    assert ref.keys() == mart.keys()
    for key, (trips, median) in ref.items():
        assert mart[key][0] == trips, key
        assert abs(mart[key][1] - median) <= 0.005 + 1e-9, key


def test_no_future_months_or_dates(dwh, as_of):
    assert scalar(dwh, "SELECT max(month) FROM mart.mart_route_card") <= as_of.replace(day=1)
    assert scalar(dwh, "SELECT max(last_price_date) FROM mart.mart_route_card") <= as_of


def test_mom_and_yoy_use_calendar_months(dwh):
    row = dwh.execute("""
        SELECT c.mom_pct, round((c.median_trip / p.median_trip - 1) * 100, 2)
        FROM mart.mart_route_card c
        JOIN mart.mart_route_card p USING (loading_city_gcid, unloading_city_gcid, body_group, weight_category)
        WHERE c.month = DATE '2026-09-01' AND p.month = DATE '2026-08-01' LIMIT 1""").fetchone()
    assert row[0] == row[1]
    # Нет ячейки за календарно предыдущий месяц -> NULL, даже если есть более ранние месяцы.
    assert scalar(dwh, """
        SELECT count(*) FROM mart.mart_route_card c
        WHERE c.mom_pct IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM mart.mart_route_card p
            WHERE p.month = (c.month - interval '1 month')::date
              AND (p.loading_city_gcid, p.unloading_city_gcid, p.body_group, p.weight_category)
                = (c.loading_city_gcid, c.unloading_city_gcid, c.body_group, c.weight_category))""") == 0
    assert scalar(dwh, """
        SELECT count(*) FROM mart.mart_route_card c
        WHERE c.mom_pct IS NULL AND EXISTS (
            SELECT 1 FROM mart.mart_route_card p
            WHERE p.month = (c.month - interval '1 month')::date AND p.median_trip <> 0
              AND (p.loading_city_gcid, p.unloading_city_gcid, p.body_group, p.weight_category)
                = (c.loading_city_gcid, c.unloading_city_gcid, c.body_group, c.weight_category))""") == 0
    assert scalar(dwh, """
        SELECT count(*) FROM mart.mart_route_card c
        WHERE c.yoy_pct IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM mart.mart_route_card p
            WHERE p.month = (c.month - interval '1 year')::date
              AND (p.loading_city_gcid, p.unloading_city_gcid, p.body_group, p.weight_category)
                = (c.loading_city_gcid, c.unloading_city_gcid, c.body_group, c.weight_category))""") == 0


def test_data_trust_metrics_rules(dwh):
    assert scalar(dwh, "SELECT count(*) FROM mart.mart_route_card WHERE trips_count < 2 AND cv_rate IS NOT NULL") == 0
    assert scalar(dwh, "SELECT count(*) FROM mart.mart_route_card WHERE carriers_count > trips_count") == 0
    assert scalar(dwh, "SELECT count(*) FROM mart.mart_route_card WHERE NOT (p25_trip <= median_trip "
                       "AND median_trip <= p75_trip AND p25_km <= median_km AND median_km <= p75_km)") == 0
    assert scalar(dwh, """
        SELECT count(*) FROM mart.mart_route_card
        WHERE reliability_level <> CASE
            WHEN trips_count >= 30 AND carriers_count >= 5 AND freshness_days <= 30 THEN 'HIGH'
            WHEN trips_count >= 10 THEN 'MEDIUM' ELSE 'LOW_DATA' END""") == 0
    assert scalar(dwh, "SELECT count(*) FROM mart.mart_route_card WHERE reliability_level = 'HIGH'") > 0


def test_carriers_count_only_carrier_roles(dwh, as_of):
    cell = dwh.execute("""SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category,
                                 carriers_count
                          FROM mart.mart_route_card ORDER BY trips_count DESC LIMIT 1""").fetchone()
    expected = scalar(dwh, """
        SELECT count(DISTINCT organization_id) FROM core.v_market_base
        WHERE month = %s AND loading_city_gcid = %s AND unloading_city_gcid = %s AND body_group = %s
          AND weight_category = %s AND price_type IN (3, 9) AND price_date <= %s""", (*cell[:5], as_of))
    assert cell[5] == expected
