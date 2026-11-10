import pytest

from tests.conftest import scalar

pytestmark = pytest.mark.db


@pytest.mark.parametrize("weight, expected", [
    (38.0, "G20T"), (30.0, "G20T"), (29.96, "G20T"), (29.94, "FTL"), (10.0, "FTL"), (9.95, "FTL"),
    (9.94, "10T"), (5.0, "10T"), (4.9, "5T"), (2.5, "5T"), (2.4, "L5T"), (0.5, "L5T"), (0.45, "L5T"),
    (0.44, "OTH"), (0.0, "OTH"),
])
def test_weight_category_boundaries(dwh, weight, expected):
    assert scalar(dwh, "SELECT core.fn_weight_category(%s)", (weight,)) == expected


@pytest.mark.parametrize("volume, expected", [(0, "OTH"), (0.1, "FTL"), (120, "FTL"), (120.01, "OTH"), (-1, "OTH")])
def test_volume_category_boundaries(dwh, volume, expected):
    assert scalar(dwh, "SELECT core.fn_volume_category(%s)", (volume,)) == expected


def test_every_raw_row_is_loaded_or_quarantined_exactly_once(dwh):
    assert scalar(dwh, """
        SELECT count(*) FROM raw.prices r
        WHERE (EXISTS (SELECT 1 FROM core.fact_price f WHERE f.price_id = r.id))
            = (EXISTS (SELECT 1 FROM dq.dq_quarantine_prices q WHERE q.price_id = r.id))""") == 0


def test_core_has_no_invalid_rows(dwh):
    assert scalar(dwh, """
        SELECT count(*) FROM core.fact_price
        WHERE price <= 0 OR distance <= 0 OR total_weight < 0 OR total_volume < 0
           OR (currency_code = '643' AND price <= 1000)
           OR price_rub IS NULL OR weight_category IS NULL OR volume_category IS NULL""") == 0
    assert scalar(dwh, """
        SELECT count(*) FROM core.fact_price f
        WHERE NOT EXISTS (SELECT 1 FROM core.dim_city c WHERE c.gc_id = f.loading_city_gcid)
           OR NOT EXISTS (SELECT 1 FROM core.dim_city c WHERE c.gc_id = f.unloading_city_gcid)""") == 0


def test_known_bad_rows_are_quarantined_with_reason(dwh):
    zero_prices = scalar(dwh, "SELECT count(*) FROM raw.prices WHERE price <= 0")
    assert zero_prices > 0
    assert scalar(dwh, "SELECT count(*) FROM dq.dq_quarantine_prices WHERE reason_code = 'PRICE_NON_POSITIVE'") \
        == zero_prices
    no_rate = scalar(dwh, "SELECT count(*) FROM raw.prices WHERE currency_code IN ('840','978') "
                          "AND nearest_rate_date IS NULL")
    assert no_rate > 0
    assert scalar(dwh, "SELECT count(*) FROM dq.dq_quarantine_prices WHERE reason_code = 'FX_RATE_DATE_MISSING'") \
        == no_rate


def test_rub_rows_without_rate_date_are_kept(dwh):
    # Для рублей курс = 1, nearest_rate_date не нужна: такие строки не должны теряться.
    assert scalar(dwh, "SELECT count(*) FROM core.fact_price WHERE currency_code = '643' AND rate_date IS NULL") > 0


def test_fx_conversion_uses_exact_rate_on_nearest_rate_date(dwh):
    assert scalar(dwh, """
        SELECT count(*) FROM core.fact_price f
        JOIN core.exchange_rate e ON e.num_code = f.currency_code AND e.setting_date = f.rate_date
        WHERE f.currency_code IN ('840', '978') AND abs(f.price_rub - f.price * e.value) > 1e-6""") == 0
    assert scalar(dwh, "SELECT count(*) FROM core.fact_price WHERE currency_code IN ('840','978')") > 0
    assert scalar(dwh, "SELECT count(*) FROM core.fact_price WHERE currency_code = '643' AND price_rub <> price") == 0


def test_city_dedup_takes_min_id_and_parses_region(dwh):
    assert scalar(dwh, "SELECT count(*) FROM raw.cities") > scalar(dwh, "SELECT count(*) FROM core.dim_city")
    kazan = dwh.execute("SELECT city_id, city_name, region_name, duplicates_cnt FROM core.dim_city "
                        "WHERE gc_id = 1003").fetchone()
    min_id = scalar(dwh, "SELECT min(id) FROM raw.cities WHERE gc_id = 1003")
    assert kazan == (min_id, "Казань", "Республика Татарстан", 2)
    assert scalar(dwh, "SELECT region_name FROM core.dim_city WHERE gc_id = 1001") == "г. Москва"


def test_body_groups_dedup_and_fallbacks(dwh):
    # Тент 56 + тент 61 у одной сделки -> одна строка «Тент».
    pid = scalar(dwh, """
        SELECT b.price_id FROM raw.price_body_types b JOIN core.fact_price f ON f.price_id = b.price_id
        GROUP BY b.price_id HAVING array_agg(b.body_type_id ORDER BY b.body_type_id) = ARRAY[56, 61] LIMIT 1""")
    assert dwh.execute("SELECT body_group FROM core.fact_price_body_group WHERE price_id = %s",
                       (pid,)).fetchall() == [("Тент",)]
    # Тент + борт -> две разные группы.
    pid = scalar(dwh, """
        SELECT b.price_id FROM raw.price_body_types b JOIN core.fact_price f ON f.price_id = b.price_id
        GROUP BY b.price_id HAVING array_agg(b.body_type_id ORDER BY b.body_type_id) = ARRAY[15, 56] LIMIT 1""")
    assert sorted(r[0] for r in dwh.execute(
        "SELECT body_group FROM core.fact_price_body_group WHERE price_id = %s", (pid,))) == ["Борт", "Тент"]
    # Дочерний код 1С (57) -> стандартная группа «Тент».
    assert scalar(dwh, """
        SELECT count(*) FROM raw.price_body_types b
        JOIN core.fact_price_body_group g ON g.price_id = b.price_id
        WHERE b.body_type_id = 57 AND g.body_group <> 'Тент'
          AND NOT EXISTS (SELECT 1 FROM raw.price_body_types b2 WHERE b2.price_id = b.price_id AND b2.body_type_id <> 57)
    """) == 0
    # Нет связей и body_type IS NULL -> «Не указан»; нет связей, но body_type есть -> группа по body_type.
    assert scalar(dwh, """
        SELECT count(*) FROM core.fact_price f
        JOIN core.fact_price_body_group g ON g.price_id = f.price_id
        WHERE NOT EXISTS (SELECT 1 FROM raw.price_body_types b WHERE b.price_id = f.price_id)
          AND g.body_group <> CASE WHEN f.body_type IS NULL THEN 'Не указан' ELSE f.body_group END""") == 0
    assert scalar(dwh, "SELECT count(*) FROM core.fact_price_body_group WHERE body_group = 'Не указан'") > 0
    # У каждой сделки core есть хотя бы одна группа и хотя бы один месяц.
    assert scalar(dwh, "SELECT count(*) FROM core.fact_price f WHERE NOT EXISTS "
                       "(SELECT 1 FROM core.fact_price_body_group g WHERE g.price_id = f.price_id)") == 0
    assert scalar(dwh, "SELECT count(*) FROM core.fact_price f WHERE NOT EXISTS "
                       "(SELECT 1 FROM core.fact_price_month m WHERE m.price_id = f.price_id)") == 0
