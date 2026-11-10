"""Feature pipeline: признаки строятся одной функцией и для обучения и для скоринга."""

import numpy as np
import pandas as pd

TARGET = "price_rub"

NUMERIC_FEATURES = [
    "distance", "total_weight", "total_volume",
    "origin_lat", "origin_lon", "dest_lat", "dest_lon",
    "year", "month", "day_of_week", "day_of_year",
    "month_sin", "month_cos", "dow_sin", "dow_cos", "doy_sin", "doy_cos",
]
CATEGORICAL_FEATURES = [
    "origin_region", "dest_region", "origin_gcid", "dest_gcid",
    "body_group", "body_type", "weight_category", "volume_category", "price_type",
]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

FORBIDDEN_FEATURES = frozenset({
    "price", "price_rub", "price_with_vat", "fx_rate",
    "price_km", "price_per_km", "price_per_ton",
    "customer_cost", "transportation_cost", "carrier_cost", "profit",
    "avg_price", "min_km_price", "max_km_price", "price_group_id",
    "outlier_mark", "outliers_amount", "clear_items_amount",
    "price_id",
})

# SQL: только core (очищенные данные) + дедуплицированный справочник городов.
DATASET_SQL = """
SELECT f.price_id,
       f.loading_date,
       f.loading_city_gcid,
       f.unloading_city_gcid,
       f.distance,
       f.total_weight,
       f.total_volume,
       f.body_type,
       f.body_group,
       f.weight_category,
       f.volume_category,
       f.price_type,
       f.outlier_mark,
       f.price_rub,
       oc.region_name AS origin_region,
       oc.latitude    AS origin_lat,
       oc.longitude   AS origin_lon,
       dc.region_name AS dest_region,
       dc.latitude    AS dest_lat,
       dc.longitude   AS dest_lon
FROM core.fact_price f
JOIN core.dim_city oc ON oc.gc_id = f.loading_city_gcid
JOIN core.dim_city dc ON dc.gc_id = f.unloading_city_gcid
WHERE f.is_fact
  AND NOT f.complex
  AND f.distance > 0
  AND f.price_rub > 0
  AND f.loading_date >= %(min_date)s
ORDER BY f.price_id
"""


def assert_no_leakage(features: list[str]) -> None:
    leaked = set(features) & FORBIDDEN_FEATURES
    if leaked:
        raise ValueError(f"Target leakage: forbidden features {sorted(leaked)}")


def _cyclic(values: pd.Series, period: float) -> tuple[pd.Series, pd.Series]:
    angle = 2 * np.pi * values / period
    return np.sin(angle), np.cos(angle)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Возвращает копию df с признаками модели. Ничего не «подсматривает» в таргет."""
    out = df.copy()
    ld = pd.to_datetime(out["loading_date"])
    out["year"] = ld.dt.year
    out["month"] = ld.dt.month
    out["day_of_week"] = ld.dt.dayofweek
    out["day_of_year"] = ld.dt.dayofyear
    out["month_sin"], out["month_cos"] = _cyclic(out["month"], 12)
    out["dow_sin"], out["dow_cos"] = _cyclic(out["day_of_week"], 7)
    out["doy_sin"], out["doy_cos"] = _cyclic(out["day_of_year"], 365.25)

    out["origin_gcid"] = out["loading_city_gcid"]
    out["dest_gcid"] = out["unloading_city_gcid"]
    for col in CATEGORICAL_FEATURES:
        out[col] = out[col].astype("object").where(out[col].notna(), "NA").astype(str)
    for col in NUMERIC_FEATURES:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype(float)
    assert_no_leakage(FEATURES)
    return out
