from datetime import date, timedelta

import pytest

from common.db import connect, run_sql_dir, set_params
from etl.transform import transform_prices
from etl.window import Window

pytestmark = pytest.mark.db

CDC_DAY = date(2099, 1, 3)
NEW_CHILD_ID = 2_200_000_000

CELL_TRIPS_SQL = """
SELECT (SELECT trips_count FROM mart.mart_route_card
        WHERE (month, loading_city_gcid, unloading_city_gcid, body_group, weight_category) = (%(m)s, %(l)s, %(u)s, %(g)s, %(w)s)),
       (SELECT count(DISTINCT price_id) FROM core.v_market_base
        WHERE (month, loading_city_gcid, unloading_city_gcid, body_group, weight_category) = (%(m)s, %(l)s, %(u)s, %(g)s, %(w)s)
          AND price_date <= %(as_of)s)
"""


@pytest.fixture(scope="module")
def cdc_result(dwh, settings, as_of):
    with connect(settings.dwh_db) as conn, conn.transaction(force_rollback=True):
        # Рублёвая сделка рыночной выборки с одним кузовом и одним месяцем.
        price_id, price, route_from, route_to, weight, old_group, old_month, old_date = conn.execute(
            "SELECT f.price_id, f.price, f.loading_city_gcid, f.unloading_city_gcid, f.weight_category, "
            "       g.body_group, m.month, m.price_date "
            "FROM core.fact_price f "
            "JOIN core.fact_price_body_group g ON g.price_id = f.price_id "
            "JOIN core.fact_price_month m ON m.price_id = f.price_id "
            "WHERE f.currency_code = '643' AND m.price_date <= %s "
            "  AND EXISTS (SELECT 1 FROM core.v_market_base b WHERE b.price_id = f.price_id) "
            "  AND (SELECT count(*) FROM core.fact_price_body_group x WHERE x.price_id = f.price_id) = 1 "
            "  AND (SELECT count(*) FROM core.fact_price_month x WHERE x.price_id = f.price_id) = 1 "
            "ORDER BY f.price_id LIMIT 1", (as_of - timedelta(days=120),)).fetchone()
        new_body_type, new_group = conn.execute(
            "SELECT body_type_id, body_group FROM core.body_group_map WHERE body_group <> %s "
            "ORDER BY body_type_id LIMIT 1", (old_group,)).fetchone()
        new_date = old_date + timedelta(days=62)

        # Новая версия сделки и весь новый набор дочерних строк — как после extract.
        conn.execute("UPDATE raw.prices SET price = %s, updated_at = %s WHERE id = %s",
                     (price * 2, CDC_DAY, price_id))
        conn.execute("DELETE FROM raw.price_body_types WHERE price_id = %s", (price_id,))
        conn.execute("DELETE FROM raw.price_dates WHERE price_id = %s", (price_id,))
        conn.execute("INSERT INTO raw.price_body_types (id, price_id, body_type_id) VALUES (%s, %s, %s)",
                     (NEW_CHILD_ID, price_id, new_body_type))
        conn.execute("INSERT INTO raw.price_dates (id, price_id, price_date) VALUES (%s, %s, %s)",
                     (NEW_CHILD_ID, price_id, new_date))

        stats = transform_prices(conn, settings, Window(CDC_DAY, CDC_DAY))
        # Витрина — тот же SQL, что в build_marts, в транзакции теста; полный пересчёт: сделка ушла из старого
        # месяца, а он может быть глубже окна инкремента.
        set_params(conn, {"mart.as_of": as_of, "mart.full": "true", "mart.lookback_months": 3})
        run_sql_dir(conn, settings.sql_dir / "transforms" / "mart")

        key = {"l": route_from, "u": route_to, "w": weight, "as_of": as_of}
        yield {
            "stats": stats,
            "expected": {"price": price * 2, "group": new_group, "month": new_date.replace(day=1)},
            "core_price": conn.execute("SELECT price, price_rub FROM core.fact_price WHERE price_id = %s",
                                       (price_id,)).fetchone(),
            "core_groups": {r[0] for r in conn.execute(
                "SELECT body_group FROM core.fact_price_body_group WHERE price_id = %s", (price_id,))},
            "core_months": {r[0] for r in conn.execute(
                "SELECT month FROM core.fact_price_month WHERE price_id = %s", (price_id,))},
            "deal_cells": set(conn.execute(
                "SELECT DISTINCT month, body_group FROM core.v_market_base WHERE price_id = %s", (price_id,))),
            "old_cell": conn.execute(CELL_TRIPS_SQL, {**key, "m": old_month, "g": old_group}).fetchone(),
            "new_cell": conn.execute(CELL_TRIPS_SQL, {**key, "m": new_date.replace(day=1), "g": new_group}).fetchone(),
        }


def test_only_the_changed_deal_is_in_window(cdc_result):
    assert cdc_result["stats"]["rows_read"] == 1
    assert cdc_result["stats"]["rows_loaded_core"] == 1


def test_new_version_replaces_old_in_core(cdc_result):
    expected = cdc_result["expected"]
    assert cdc_result["core_price"] == (expected["price"], expected["price"])        # RUB: курс 1
    assert cdc_result["core_groups"] == {expected["group"]}
    assert cdc_result["core_months"] == {expected["month"]}


def test_mart_counts_deal_only_in_new_cell(cdc_result):
    expected = cdc_result["expected"]
    old_trips, old_expected = cdc_result["old_cell"]
    new_trips, new_expected = cdc_result["new_cell"]
    assert cdc_result["deal_cells"] == {(expected["month"], expected["group"])}
    assert (old_trips or 0) == old_expected          # старая ячейка пересчитана без сделки (или исчезла)
    assert new_trips == new_expected >= 1            # новая ячейка учитывает сделку
