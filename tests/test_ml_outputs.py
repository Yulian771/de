from datetime import date

import pytest

from tests.conftest import scalar

pytestmark = pytest.mark.db


def test_predictions_cover_test_and_future_of_primary_model_only(dwh, as_of):
    rows = dict(dwh.execute("SELECT split, count(*) FROM mart.ml_price_predictions GROUP BY split").fetchall())
    assert set(rows) == {"test", "future"} and min(rows.values()) > 0
    # В таблице прогнозов только основная модель; baselines — только метрики.
    assert scalar(dwh, "SELECT array_agg(DISTINCT model_name) FROM mart.ml_price_predictions") == ["catboost"]
    assert scalar(dwh, "SELECT count(*) FROM mart.ml_price_predictions WHERE predicted_price_rub <= 0") == 0
    lo, hi = dwh.execute("SELECT min(loading_date), max(loading_date) FROM mart.ml_price_predictions "
                         "WHERE split = 'test'").fetchone()
    assert lo >= date(2026, 10, 5) and hi <= as_of
    assert scalar(dwh, "SELECT min(loading_date) FROM mart.ml_price_predictions WHERE split = 'future'") \
        > as_of


def test_future_rows_have_no_actuals_even_if_source_price_exists(dwh):
    assert scalar(dwh, """
        SELECT count(*) FROM mart.ml_price_predictions
        WHERE split = 'future' AND (actual_price_rub IS NOT NULL OR abs_error_rub IS NOT NULL OR ape_pct IS NOT NULL)
    """) == 0
    # в источнике у этих заявок цена заполнена
    assert scalar(dwh, """
        SELECT count(*) FROM mart.ml_price_predictions p JOIN core.fact_price f USING (price_id)
        WHERE p.split = 'future' AND f.price > 0""") > 0
    assert scalar(dwh, "SELECT count(*) FROM mart.ml_price_predictions WHERE split = 'test' "
                       "AND (actual_price_rub IS NULL OR ape_pct IS NULL)") == 0


def test_errors_are_consistent(dwh):
    assert scalar(dwh, """
        SELECT count(*) FROM mart.ml_price_predictions
        WHERE split = 'test' AND abs(abs_error_rub - abs(actual_price_rub - predicted_price_rub)) > 0.011""") == 0


def test_metrics_written_for_all_models(dwh, as_of):
    rows = {r[0]: r[1:] for r in dwh.execute(
        "SELECT model_name, r2, mape_pct, rmse_rub, mae_rub FROM mart.ml_model_metrics WHERE as_of = %s",
        (as_of,))}
    assert {"catboost", "ridge", "decision_tree"} <= rows.keys()
    r2, mape, rmse, mae = rows["catboost"]
    # Санити-проверка: R² ~1.0 означал бы утечку, слишком низкий — сломанные признаки.
    assert 0.85 < r2 < 0.99
    assert 0 < mape < 25 and rmse > mae > 0
    assert rows["catboost"][0] >= rows["ridge"][0]          # R² основной модели выше бейзлайна
    assert rows["catboost"][1] <= rows["ridge"][1]          # MAPE ниже


def test_predictions_carry_version_of_trained_model(dwh, as_of):
    """Все прогнозы — от одной версии модели, и это версия из метрик обучения на дату as_of."""
    versions, prediction_version, metrics_version = dwh.execute("""
        SELECT count(DISTINCT p.model_version), min(p.model_version), min(m.model_version)
        FROM mart.ml_price_predictions p
        JOIN mart.ml_model_metrics m ON m.model_name = p.model_name AND m.as_of = %s AND m.split = 'test'
        """, (as_of,)).fetchone()
    assert versions == 1 and prediction_version == metrics_version
    assert prediction_version.startswith(f"{as_of:%Y%m%d}-")


def test_future_requests_get_latest_available_corridor(dwh, as_of):
    """Будущих месяцев в витрине нет, поэтому коридор будущей заявки — последний месяц ячейки до as_of."""
    total, with_corridor, max_month = dwh.execute("""
        SELECT count(*), count(median_trip), max(corridor_month)
        FROM mart.v_fact_vs_forecast WHERE split = 'future'""").fetchone()
    assert with_corridor > total * 0.8
    assert max_month <= as_of.replace(day=1)
