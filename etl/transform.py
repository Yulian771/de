"""Transform: raw -> core + карантин dq 
(Преобразование выполняется SQL-запросами внутри DWH над всеми сделками окна сразу).

  * справочники core (sql/transforms/core_dimensions) — один раз за запуск, отдельной транзакцией;
  * сделки (sql/transforms/core_prices) — сделки raw с updated_at в [start, end]; транзакция на
    календарный месяц окна. В конце каждой транзакции — сверка: прочитано = загружено в core + в карантине.

Блокировки: разделяемая raw и эксклюзивная core.
"""

import psycopg
from psycopg import sql

from common.config import Settings
from common.db import connect, lock, run_sql_dir, set_params, step_run
from common.log import get_logger
from etl.window import Window, month_windows, updated_at_range

log = get_logger(__name__)


def raw_window(settings: Settings) -> Window:
    """Диапазон updated_at в raw — окно полной трансформации (пересборка core из raw)."""
    with connect(settings.dwh_db, autocommit=True) as conn:
        return updated_at_range(conn, sql.Identifier("raw", "prices"))


def run_transform(settings: Settings, window: Window) -> dict:
    """Справочники core, затем сделки окна по месяцам. Каждая транзакция — своя запись в etl.run_log"""
    with connect(settings.dwh_db) as conn:
        with step_run(settings.dwh_db, "transform_dimensions") as run, conn.transaction():
            _lock_layers(conn)
            run_sql_dir(conn, settings.sql_dir / "transforms" / "core_dimensions")
            run.stats.update(_dimension_counts(conn))
        log.info("transform.dimensions.done", extra={"run_id": run.run_id, **run.stats})

        totals = {"windows": 0, "rows_read": 0, "rows_loaded_core": 0, "rows_quarantined": 0}
        for month in month_windows(window):
            with step_run(settings.dwh_db, "transform_prices", month.start, month.end) as run, conn.transaction():
                _lock_layers(conn)
                run.stats.update(transform_prices(conn, settings, month))
            log.info("transform.prices.done",
                     extra={"run_id": run.run_id, "start": month.start, "end": month.end, **run.stats})
            totals["windows"] += 1
            for key in ("rows_read", "rows_loaded_core", "rows_quarantined"):
                totals[key] += run.stats[key]
    log.info("transform.done", extra={"start": window.start, "end": window.end, **totals})
    return totals


def transform_prices(conn: psycopg.Connection, settings: Settings, window: Window) -> dict:
    """Сделки raw -> core / карантин и сверка — внутри транзакции"""
    # conn.execute("SET LOCAL jit = off")
    set_params(conn, {"etl.window_start": window.start, "etl.window_end_excl": window.end_excl})
    run_sql_dir(conn, settings.sql_dir / "transforms" / "core_prices")
    return _reconcile(conn, window)


def _lock_layers(conn: psycopg.Connection) -> None:
    """Блокировки слоев"""
    lock(conn, "raw", shared=True)
    lock(conn, "core")


def _dimension_counts(conn: psycopg.Connection) -> dict:
    """Число строк в справочниках core после их обновления"""
    return dict(zip(("dim_city", "dim_body_type", "exchange_rate"), conn.execute(
        "SELECT (SELECT count(*) FROM core.dim_city), (SELECT count(*) FROM core.dim_body_type), "
        "(SELECT count(*) FROM core.exchange_rate)").fetchone()))


def _reconcile(conn: psycopg.Connection, window: Window) -> dict:
    """Сверка окна: каждая сделка raw из окна либо в core, либо в карантине. Иначе — откат транзакции."""
    read = conn.execute(
        "SELECT count(*) FROM raw.prices WHERE updated_at >= %(start)s AND updated_at < %(end_excl)s",
        window.params).fetchone()[0]
    loaded, w_filled, v_filled = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE f.weight_category_filled), "
        "count(*) FILTER (WHERE f.volume_category_filled) "
        "FROM core.fact_price f JOIN stg_prices s ON s.id = f.price_id").fetchone()
    by_reason = dict(conn.execute(
        "SELECT q.reason_code, count(*) FROM dq.dq_quarantine_prices q JOIN stg_prices s ON s.id = q.price_id "
        "GROUP BY 1 ORDER BY 1").fetchall())
    quarantined = conn.execute(
        "SELECT count(DISTINCT q.price_id) FROM dq.dq_quarantine_prices q JOIN stg_prices s ON s.id = q.price_id"
    ).fetchone()[0]
    if read != loaded + quarantined:
        raise RuntimeError(f"DQ reconciliation failed for {window.start}..{window.end}: "
                           f"read={read} loaded={loaded} quarantined={quarantined}")
    return {
        "rows_read": read,
        "rows_loaded_core": loaded,
        "rows_quarantined": quarantined,
        "quarantine_by_reason": by_reason,
        "weight_category_filled": w_filled,
        "volume_category_filled": v_filled,
    }
