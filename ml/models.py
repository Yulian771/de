"""Модели: CatBoost (основная) и бейзлайны scikit-learn. Все трансформеры обучаются только на train."""

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor, Pool
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler
from sklearn.tree import DecisionTreeRegressor

from ml.features import CATEGORICAL_FEATURES, FEATURES, NUMERIC_FEATURES
from ml.split import TimeSplit

CATBOOST_PARAMS = {
    "loss_function": "RMSE",
    "iterations": 3000,
    "learning_rate": 0.05,
    "depth": 8,
    "l2_leaf_reg": 3.0,
    "od_type": "Iter",
    "od_wait": 200,
}


@dataclass
class ModelArtifact:
    """Обученная модель с метаданными: по split скоринг размечает выборку, по features проверяет совместимость."""
    name: str
    version: str              # <as_of>-<время обучения UTC>, например 20261026-20261110T081500Z
    split: TimeSplit
    trained_at: datetime
    features: list[str]
    model: "CatBoostModel | SklearnModel"

    @staticmethod
    def make_version(split: TimeSplit, trained_at: datetime) -> str:
        return f"{split.as_of:%Y%m%d}-{trained_at:%Y%m%dT%H%M%SZ}"


class CatBoostModel:
    name = "catboost"

    def __init__(self, seed: int):
        self.seed = seed
        self.model: CatBoostRegressor | None = None
        self.best_iterations: int | None = None

    def fit(self, train: pd.DataFrame, y: pd.Series, val_mask: pd.Series) -> "CatBoostModel":
        # 1) подбираем число итераций по хронологическому хвосту train;
        # 2) переобучаем на всём train с найденным числом итераций.
        probe = CatBoostRegressor(**CATBOOST_PARAMS, random_seed=self.seed, verbose=0, allow_writing_files=False)
        probe.fit(Pool(train.loc[~val_mask, FEATURES], y[~val_mask], cat_features=CATEGORICAL_FEATURES),
                  eval_set=Pool(train.loc[val_mask, FEATURES], y[val_mask], cat_features=CATEGORICAL_FEATURES),
                  use_best_model=True)
        self.best_iterations = int(probe.get_best_iteration()) + 1
        params = {k: v for k, v in CATBOOST_PARAMS.items() if k not in ("od_type", "od_wait")}
        params["iterations"] = self.best_iterations
        self.model = CatBoostRegressor(**params, random_seed=self.seed, verbose=0, allow_writing_files=False)
        self.model.fit(Pool(train[FEATURES], y, cat_features=CATEGORICAL_FEATURES))
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.model.predict(Pool(df[FEATURES], cat_features=CATEGORICAL_FEATURES))

    def params(self) -> dict:
        return {**CATBOOST_PARAMS, "best_iterations": self.best_iterations, "random_seed": self.seed}

    def feature_importance(self) -> dict[str, float]:
        values = self.model.get_feature_importance()
        return dict(sorted(zip(FEATURES, map(float, values)), key=lambda kv: -kv[1]))


class SklearnModel:
    def __init__(self, name: str, pipeline: Pipeline, params: dict):
        self.name = name
        self.pipeline = pipeline
        self._params = params

    def fit(self, train: pd.DataFrame, y: pd.Series, val_mask: pd.Series | None = None) -> "SklearnModel":
        self.pipeline.fit(train[FEATURES], y)
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return self.pipeline.predict(df[FEATURES])

    def params(self) -> dict:
        return self._params


def ridge_baseline(seed: int) -> SklearnModel:
    pre = ColumnTransformer([
        ("num", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]),
         NUMERIC_FEATURES),
        ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=5), CATEGORICAL_FEATURES),
    ])
    params = {"alpha": 1.0}
    return SklearnModel("ridge", Pipeline([("pre", pre), ("model", Ridge(**params))]), params)


def tree_baseline(seed: int) -> SklearnModel:
    pre = ColumnTransformer([
        ("num", SimpleImputer(strategy="median"), NUMERIC_FEATURES),
        ("cat", OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1), CATEGORICAL_FEATURES),
    ])
    params = {"max_depth": 14, "min_samples_leaf": 20, "random_state": seed}
    return SklearnModel("decision_tree", Pipeline([("pre", pre), ("model", DecisionTreeRegressor(**params))]), params)
