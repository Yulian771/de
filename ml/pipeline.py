"""ML: обучение и скоринг с записью в mart.ml_price_predictions.

Модель сохраняется вместе с метаданными (ModelArtifact): версия, разбиение на дату обучения as_of, признаки.
Скоринг размечает сделки по as_of модели (test — её отложенный период) и по дате скоринга (future — заявки
после неё); сделки между ними — live: модель их не видела, факт уже известен.
"""

import json
import os
import random
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

from common.config import Settings
from common.db import connect, step_run
from common.log import get_logger
from ml.features import DATASET_SQL, FEATURES, TARGET, build_features
from ml.metrics import regression_metrics
from ml.models import CatBoostModel, ModelArtifact, ridge_baseline, tree_baseline
from ml.split import TimeSplit

log = get_logger(__name__)

PRIMARY_MODEL = "catboost"


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


def load_dataset(settings: Settings, min_date: date) -> pd.DataFrame:
    with connect(settings.dwh_db, autocommit=True) as conn:
        cur = conn.execute(DATASET_SQL, {"min_date": min_date})
        columns = [c.name for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=columns)
    if df.empty:
        raise RuntimeError("ML dataset is empty: run ETL first")
    return build_features(df)


def _atomic_dump(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    os.close(fd)
    joblib.dump(obj, tmp)
    os.replace(tmp, path)


def train(settings: Settings, as_of: date) -> dict:
    """Обучение на разбиении относительно as_of: train [as_of - 3 мес, as_of - 3 нед), test [as_of - 3 нед, as_of]."""
    _seed_everything(settings.random_seed)
    split = TimeSplit.from_as_of(as_of)
    with step_run(settings.dwh_db, "ml_train") as run:
        df = load_dataset(settings, split.train_start)
        masks = split.masks(df["loading_date"])
        train_df = df[masks["train"] & ~df["outlier_mark"]].reset_index(drop=True)
        test_df = df[masks["test"] & ~df["outlier_mark"]].reset_index(drop=True)
        val_mask = pd.to_datetime(train_df["loading_date"]).dt.date >= split.val_start
        log.info("ml.split", extra={
            "train_start": split.train_start, "test_start": split.test_start, "as_of": split.as_of,
            "train_rows": len(train_df), "val_rows_inside_train": int(val_mask.sum()), "test_rows": len(test_df),
            "train_max_date": train_df["loading_date"].max(), "test_min_date": test_df["loading_date"].min(),
        })
        if train_df.empty or test_df.empty:
            raise RuntimeError(f"Empty train or test for as_of {as_of}: train={len(train_df)} test={len(test_df)}")
        assert train_df["loading_date"].max() < test_df["loading_date"].min(), "temporal leakage"

        trained_at = datetime.now(timezone.utc).replace(microsecond=0)
        version = ModelArtifact.make_version(split, trained_at)
        results = {}
        for model in (CatBoostModel(settings.random_seed), ridge_baseline(settings.random_seed),
                      tree_baseline(settings.random_seed)):
            model.fit(train_df, train_df[TARGET], val_mask)
            metrics = regression_metrics(test_df[TARGET], model.predict(test_df))
            results[model.name] = {"metrics": metrics, "params": model.params()}
            _atomic_dump(ModelArtifact(name=model.name, version=version, split=split, trained_at=trained_at,
                                       features=list(FEATURES), model=model),
                         settings.model_dir / f"{model.name}.joblib")
            log.info("ml.model.trained", extra={"model": model.name, "model_version": version, **metrics})
            if isinstance(model, CatBoostModel):
                results[model.name]["feature_importance"] = model.feature_importance()

        _write_metrics(settings, split, version, results, len(train_df), trained_at)
        report = {
            "model_version": version, "as_of": split.as_of.isoformat(), "train_start": split.train_start.isoformat(),
            "test_start": split.test_start.isoformat(), "trained_at": trained_at.isoformat(),
            "train_rows": len(train_df), "test_rows": len(test_df), "features": FEATURES, "models": results,
        }
        (settings.model_dir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        run.stats.update({"model_version": version, **{name: r["metrics"] for name, r in results.items()}})
    return report


def _write_metrics(settings: Settings, split: TimeSplit, version: str, results: dict, train_rows: int,
                   trained_at: datetime) -> None:
    rows = [(name, split.as_of, "test", version, r["metrics"]["n_rows"], r["metrics"]["r2"], r["metrics"]["mape_pct"],
             r["metrics"]["rmse_rub"], r["metrics"]["mae_rub"], train_rows, Jsonb(r["params"]), trained_at)
            for name, r in results.items()]
    with connect(settings.dwh_db) as conn, conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO mart.ml_model_metrics
                   (model_name, as_of, split, model_version, n_rows, r2, mape_pct, rmse_rub, mae_rub, train_rows,
                    params, trained_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (model_name, as_of, split) DO UPDATE SET
                   model_version = EXCLUDED.model_version, n_rows = EXCLUDED.n_rows, r2 = EXCLUDED.r2,
                   mape_pct = EXCLUDED.mape_pct, rmse_rub = EXCLUDED.rmse_rub, mae_rub = EXCLUDED.mae_rub,
                   train_rows = EXCLUDED.train_rows, params = EXCLUDED.params, trained_at = EXCLUDED.trained_at""",
            rows)


def load_artifact(settings: Settings, name: str) -> ModelArtifact:
    path = settings.model_dir / f"{name}.joblib"
    if not path.exists():
        raise FileNotFoundError(f"Model {path} not found: run `ml train` first")
    artifact = joblib.load(path)
    if not isinstance(artifact, ModelArtifact):
        raise RuntimeError(f"Model {path} has no metadata (old format): run `ml train` again")
    if artifact.features != FEATURES:
        raise RuntimeError(f"Model {name} {artifact.version} was trained on other features: run `ml train` again")
    return artifact


def score(settings: Settings, as_of: date, models: tuple[str, ...] = (PRIMARY_MODEL, "ridge", "decision_tree")) -> dict:
    """Скоринг на дату as_of: test и live (с фактом) и future."""
    with step_run(settings.dwh_db, "ml_score") as run:
        artifacts = [load_artifact(settings, name) for name in models]
        datasets: dict[date, pd.DataFrame] = {}
        scored_at = datetime.now(timezone.utc)

        with connect(settings.dwh_db) as conn, conn.transaction():
            for artifact in artifacts:
                min_date = artifact.split.test_start
                if min_date not in datasets:
                    datasets[min_date] = load_dataset(settings, min_date)
                df = datasets[min_date]
                labels = artifact.split.scoring_labels(df["loading_date"], as_of)
                scope = df[labels.notna()].copy()
                scope["split"] = labels[labels.notna()]

                pred = np.round(artifact.model.predict(scope), 2)
                has_actual = scope["split"].to_numpy() != "future"
                actual = np.where(has_actual, scope[TARGET].round(2), np.nan)   # для будущих — NULL
                abs_err = np.abs(actual - pred)
                ape = abs_err / actual * 100

                name = artifact.name
                conn.execute("DELETE FROM mart.ml_price_predictions WHERE model_name = %s", (name,))
                with conn.cursor().copy(
                    "COPY mart.ml_price_predictions (price_id, model_name, model_version, split, loading_date, "
                    "loading_city_gcid, unloading_city_gcid, body_group, weight_category, distance, outlier_mark, "
                    "actual_price_rub, predicted_price_rub, abs_error_rub, ape_pct, scored_at) FROM STDIN"
                ) as copy:
                    n = len(scope)
                    for row in zip(scope["price_id"], [name] * n, [artifact.version] * n, scope["split"],
                                   scope["loading_date"], scope["loading_city_gcid"], scope["unloading_city_gcid"],
                                   scope["body_group"], scope["weight_category"], scope["distance"],
                                   scope["outlier_mark"], _nullable(actual), pred, _nullable(abs_err),
                                   _nullable(np.round(ape, 2)), [scored_at] * n):
                        copy.write_row(row)
                counts = scope["split"].value_counts()
                run.stats[name] = {"model_version": artifact.version, "model_as_of": artifact.split.as_of,
                                   **{f"{s}_rows": int(counts.get(s, 0)) for s in ("test", "live", "future")}}
                log.info("ml.scored", extra={"model": name, "as_of": as_of, **run.stats[name]})
    return run.stats


def _nullable(values: np.ndarray) -> list:
    return [None if np.isnan(v) else float(v) for v in values]
