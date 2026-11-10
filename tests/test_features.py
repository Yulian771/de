from datetime import date

import numpy as np
import pandas as pd
import pytest

from ml.features import FEATURES, FORBIDDEN_FEATURES, assert_no_leakage, build_features
from ml.split import TimeSplit


def test_no_forbidden_features_in_model():
    assert not set(FEATURES) & FORBIDDEN_FEATURES
    for leaked in ("price_with_vat", "price_km", "avg_price", "price_group_id", "outlier_mark", "price_rub"):
        with pytest.raises(ValueError):
            assert_no_leakage(FEATURES + [leaked])


def test_time_split_matches_task_boundaries():
    split = TimeSplit.from_as_of(date(2026, 10, 26))
    assert split.train_start == date(2026, 7, 26)
    assert split.test_start == date(2026, 10, 5)
    assert split.val_start < split.test_start

    days = pd.Series(pd.to_datetime(["2026-07-25", "2026-07-26", "2026-10-04", "2026-10-05",
                                     "2026-10-26", "2026-10-27"]))
    m = split.masks(days)
    assert m["train"].tolist() == [False, True, True, False, False, False]
    assert m["test"].tolist() == [False, False, False, True, True, False]
    assert m["future"].tolist() == [False, False, False, False, False, True]
    # Граница 2026-10-05 — только в test, пересечений нет.
    assert not (m["train"] & m["test"]).any()


def test_scoring_requires_model_as_of():
    """Скоринг — только на дату снимка, на которую обучена модель; на другую дату — переобучить."""
    split = TimeSplit.from_as_of(date(2026, 10, 26))
    split.require_as_of(date(2026, 10, 26))
    with pytest.raises(ValueError, match="ml train --as-of 2026-10-20"):
        split.require_as_of(date(2026, 10, 20))


def _row(loading_date: str) -> dict:
    return {"loading_date": loading_date, "loading_city_gcid": 1001, "unloading_city_gcid": 1003,
            "distance": 820.0, "total_weight": 20.0, "total_volume": 82.0, "body_type": None,
            "body_group": "Не указан", "weight_category": "FTL", "volume_category": "FTL", "price_type": 3,
            "origin_region": "г. Москва", "origin_lat": 55.7, "origin_lon": 37.6,
            "dest_region": "Республика Татарстан", "dest_lat": 55.8, "dest_lon": 49.1}


def test_cyclic_encoding_keeps_year_boundary_close():
    df = build_features(pd.DataFrame([_row("2025-12-31"), _row("2026-01-01"), _row("2026-07-01")]))
    dec, jan, jul = (df.loc[i, ["doy_sin", "doy_cos"]].to_numpy(dtype=float) for i in range(3))
    assert np.linalg.norm(dec - jan) < 0.05
    assert np.linalg.norm(dec - jul) > 1.5
    for col in ("month_sin", "month_cos", "dow_sin", "dow_cos", "doy_sin", "doy_cos"):
        assert df[col].between(-1, 1).all()


def test_categorical_nulls_are_explicit():
    df = build_features(pd.DataFrame([_row("2026-10-01")]))
    assert df.loc[0, "body_type"] == "NA"
    assert df.loc[0, "price_type"] == "3"
