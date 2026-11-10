"""Витрины mart — пересчёт месяцев из core одной транзакцией.

Инкремент: последние N месяцев до as_of + незавершённые и отсутствующие в витрине месяцы
+ соседи по MoM / YoY (правила — в sql/transforms/mart/10_route_card.sql). На пустой витрине это все месяцы.

full=True — все месяцы: периодически (например, еженедельный DAG), для backfill или после правки правил витрины.

Блокировки: разделяемая core (core не меняется посреди пересчёта) и эксклюзивная mart.
"""

from datetime import date

from common.config import Settings
from common.db import connect, lock, run_sql_dir, set_params, step_run
from common.log import get_logger

log = get_logger(__name__)


def build_marts(settings: Settings, as_of: date, full: bool = False) -> dict:
    """Пересчитывает месяцы витрины на дату снимка as_of: инкрементально или (full=True) все."""
    with step_run(settings.dwh_db, "marts") as run, connect(settings.dwh_db) as conn:
        with conn.transaction():
            lock(conn, "core", shared=True)
            lock(conn, "mart")
            set_params(conn, {"mart.as_of": as_of, "mart.full": "true" if full else "false",
                              "mart.lookback_months": settings.mart_lookback_months})
            run_sql_dir(conn, settings.sql_dir / "transforms" / "mart")
            months = conn.execute("SELECT count(*), min(month), max(month) FROM mart_months").fetchone()
            totals = conn.execute(
                "SELECT count(*), sum(trips_count), "
                "count(*) FILTER (WHERE reliability_level = 'HIGH'), "
                "count(*) FILTER (WHERE reliability_level = 'MEDIUM'), "
                "count(*) FILTER (WHERE reliability_level = 'LOW_DATA') "
                "FROM mart.mart_route_card WHERE month IN (SELECT month FROM mart_months)").fetchone()
        run.stats.update({
            "as_of": as_of, "full": full, "lookback_months": settings.mart_lookback_months,
            "months_recomputed": months[0], "min_month": months[1], "max_month": months[2],
            "cells_written": totals[0], "trips_in_cells": totals[1],
            "cells_high": totals[2], "cells_medium": totals[3], "cells_low_data": totals[4],
        })
    log.info("marts.done", extra={"run_id": run.run_id, **run.stats})
    return run.stats
