from datetime import date, datetime

import pytest

from common.db import connect
from etl.transform import transform_prices
from etl.window import Window

pytestmark = pytest.mark.db

ID_BASE = 2_100_000_000
TEST_DAY = date(2099, 1, 1)          # окно тестовых сделок
FIX_DAY = date(2099, 1, 2)           # окно исправленной версии сделки
BOUNDARY_ID = ID_BASE + 1_000        # сделка ровно на границе: updated_at = FIX_DAY 00:00 — уже не окно TEST_DAY

BASE_ROW = {
    "created_at": datetime(2026, 9, 10, 12, 0), "updated_at": datetime(2099, 1, 1, 12, 0),
    "organization_id": 100001, "loading_date": date(2026, 9, 15), "end_loading_date": date(2026, 9, 15),
    "loading_city_gcid": 1001, "unloading_city_gcid": 1003, "distance": 820.0,
    "total_weight": 20.0, "total_volume": 82.0, "body_type": 56,
    "price": 60000.0, "currency_code": "643", "complex": False, "price_type": 3, "is_fact": True,
    "nearest_rate_date": None, "outlier_mark": False, "weight_category": "FTL", "volume_category": "FTL",
}

# Дата, на которую в справочнике есть курсы USD и EUR (подставляется фикстурой).
RATE_DATE = "rate_date"

# имя случая: (отличия от BASE_ROW, ожидаемые коды причин; пустое множество — сделка валидна)
CASES: dict[str, tuple[dict, set[str]]] = {
    "valid_rub": ({}, set()),
    "valid_usd": ({"currency_code": "840", "price": 700.0, "nearest_rate_date": RATE_DATE}, set()),
    "usd_price_below_1000_is_valid": ({"currency_code": "840", "price": 900.0, "nearest_rate_date": RATE_DATE}, set()),
    "rub_without_rate_date_is_valid": ({"nearest_rate_date": None}, set()),
    "weight_category_null_is_filled": ({"weight_category": None, "total_weight": 4.9}, set()),
    "price_zero": ({"price": 0.0}, {"PRICE_NON_POSITIVE"}),
    "price_negative": ({"price": -5000.0}, {"PRICE_NON_POSITIVE"}),
    "rub_price_below_min": ({"price": 900.0}, {"PRICE_RUB_BELOW_MIN"}),
    "rub_price_exactly_1000": ({"price": 1000.0}, {"PRICE_RUB_BELOW_MIN"}),
    "distance_zero": ({"distance": 0.0}, {"DISTANCE_NON_POSITIVE"}),
    "weight_negative": ({"total_weight": -1.0, "weight_category": None}, {"WEIGHT_NEGATIVE"}),
    "volume_negative": ({"total_volume": -1.0, "volume_category": None}, {"VOLUME_NEGATIVE"}),
    "loading_city_unknown": ({"loading_city_gcid": 999_999}, {"LOADING_CITY_UNKNOWN"}),
    "unloading_city_unknown": ({"unloading_city_gcid": 999_999}, {"UNLOADING_CITY_UNKNOWN"}),
    "currency_unknown": ({"currency_code": "156"}, {"CURRENCY_UNKNOWN"}),
    "fx_rate_date_missing": ({"currency_code": "978", "price": 700.0, "nearest_rate_date": None},
                             {"FX_RATE_DATE_MISSING"}),
    "fx_rate_not_found": ({"currency_code": "978", "price": 700.0, "nearest_rate_date": date(1999, 1, 1)},
                          {"FX_RATE_NOT_FOUND"}),
    "required_field_null": ({"price": None}, {"REQUIRED_FIELD_NULL"}),
    "several_reasons": ({"price": 0.0, "distance": 0.0}, {"PRICE_NON_POSITIVE", "DISTANCE_NON_POSITIVE"}),
}
CASE_IDS = {name: ID_BASE + i for i, name in enumerate(CASES)}
COLUMNS = ["id", *BASE_ROW]


def _insert_raw(conn, rows: dict[int, dict]) -> None:
    cols = ", ".join(COLUMNS)
    marks = ", ".join(["%s"] * len(COLUMNS))
    with conn.cursor() as cur:
        cur.executemany(f"INSERT INTO raw.prices ({cols}) VALUES ({marks})",
                        [[pid, *[row[c] for c in BASE_ROW]] for pid, row in rows.items()])
        cur.executemany("INSERT INTO raw.price_dates (id, price_id, price_date) VALUES (%s, %s, %s)",
                        [(pid, pid, row["loading_date"]) for pid, row in rows.items()])
        cur.executemany("INSERT INTO raw.price_body_types (id, price_id, body_type_id) VALUES (%s, %s, %s)",
                        [(pid, pid, row["body_type"]) for pid, row in rows.items()])


def _run_transform(conn, settings, day: date) -> dict:
    conn.execute("DROP TABLE IF EXISTS stg_prices")   # второй прогон в той же транзакции теста
    return transform_prices(conn, settings, Window(day, day))


def _outcome(conn, ids: list[int]) -> dict[int, dict]:
    core = dict(conn.execute(
        "SELECT price_id, json_build_object('price_rub', price_rub, 'weight_category', weight_category, "
        "'fx_rate', fx_rate) FROM core.fact_price WHERE price_id = ANY(%s)", (ids,)).fetchall())
    reasons: dict[int, set[str]] = {}
    for pid, code in conn.execute(
            "SELECT price_id, reason_code FROM dq.dq_quarantine_prices WHERE price_id = ANY(%s)", (ids,)):
        reasons.setdefault(pid, set()).add(code)
    return {pid: {"core": core.get(pid), "reasons": reasons.get(pid, set())} for pid in ids}


@pytest.fixture(scope="module")
def dq_results(dwh, settings):
    """Один прогон трансформаций по всем случаям + повторная загрузка исправленной сделки; затем откат."""
    with connect(settings.dwh_db) as conn, conn.transaction(force_rollback=True):
        rate_date, usd_rate = conn.execute(
            "SELECT u.setting_date, u.value FROM core.exchange_rate u "
            "JOIN core.exchange_rate e ON e.setting_date = u.setting_date AND e.num_code = '978' "
            "WHERE u.num_code = '840' ORDER BY u.setting_date DESC LIMIT 1").fetchone()
        rows = {}
        for name, (overrides, _) in CASES.items():
            row = {**BASE_ROW, **overrides}
            if row["nearest_rate_date"] == RATE_DATE:
                row["nearest_rate_date"] = rate_date
            rows[CASE_IDS[name]] = row
        _insert_raw(conn, {**rows, BOUNDARY_ID: {**BASE_ROW, "updated_at": datetime(2099, 1, 2, 0, 0)}})
        stats = _run_transform(conn, settings, TEST_DAY)
        first = _outcome(conn, list(rows))
        boundary_after_first = _outcome(conn, [BOUNDARY_ID])[BOUNDARY_ID]

        # Источник исправил сделку с нулевой ценой: новая версия приходит в следующем окне.
        fixed_id = CASE_IDS["price_zero"]
        conn.execute("UPDATE raw.prices SET price = 60000, updated_at = %s WHERE id = %s", (FIX_DAY, fixed_id))
        _run_transform(conn, settings, FIX_DAY)
        after_fix = _outcome(conn, [fixed_id])[fixed_id]
        boundary_after_fix = _outcome(conn, [BOUNDARY_ID])[BOUNDARY_ID]
        yield {"first": first, "stats": stats, "after_fix": after_fix, "usd_rate": usd_rate,
               "boundary": (boundary_after_first, boundary_after_fix)}


@pytest.mark.parametrize("case", list(CASES))
def test_rule_classifies_row(dq_results, case):
    expected = CASES[case][1]
    result = dq_results["first"][CASE_IDS[case]]
    assert result["reasons"] == expected
    if expected:
        assert result["core"] is None, "отбракованная сделка не должна попасть в core"
    else:
        assert result["core"] is not None, "валидная сделка должна попасть в core"


def test_window_reconciliation_counts_every_case(dq_results):
    stats = dq_results["stats"]
    invalid = sum(1 for _, expected in CASES.values() if expected)
    assert stats["rows_read"] == len(CASES)
    assert (stats["rows_loaded_core"], stats["rows_quarantined"]) == (len(CASES) - invalid, invalid)


def test_window_is_half_open(dq_results):
    """Полночь следующего дня принадлежит следующему окну: [start 00:00, end+1 00:00)."""
    in_first, in_next = dq_results["boundary"]
    assert in_first["core"] is None and in_first["reasons"] == set()
    assert in_next["core"] is not None


def test_valid_usd_converted_by_exact_rate(dq_results):
    core = dq_results["first"][CASE_IDS["valid_usd"]]["core"]
    assert core["price_rub"] == pytest.approx(700.0 * dq_results["usd_rate"])
    rub = dq_results["first"][CASE_IDS["valid_rub"]]["core"]
    assert rub["price_rub"] == 60000.0 and rub["fx_rate"] == 1.0


def test_missing_weight_category_is_filled(dq_results):
    assert dq_results["first"][CASE_IDS["weight_category_null_is_filled"]]["core"]["weight_category"] == "5T"


def test_record_fixed_in_source_leaves_quarantine(dq_results):
    after_fix = dq_results["after_fix"]
    assert after_fix["reasons"] == set()
    assert after_fix["core"] is not None and after_fix["core"]["price_rub"] == 60000.0
