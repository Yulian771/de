"""Extract: источник deals_bigdata -> raw.

  * справочники — полный снимок, один раз за запуск, отдельной транзакцией;
  * сделки — созданные или изменённые в [start, end] (по updated_at); перезаписываются в raw
    по id вместе со всем набором дочерних строк. Транзакция на календарный месяц окна.

Допущения для тестового задания, подразумеваем, что:
  * prices.updated_at обновляется при любом изменении сделки, включая её дочерние строки;
  * физические удаления в источнике не отслеживаются.
"""

import psycopg
from psycopg import sql

from common.config import Settings
from common.db import connect, lock, step_run
from common.log import get_logger
from etl.window import Window, month_windows, updated_at_range

log = get_logger(__name__)

DICTIONARIES = ("cities", "body_types", "exchange_rates")
PRICE_CHILDREN = ("price_body_types", "price_dates")

CHANGED_IN_WINDOW = sql.SQL("p.updated_at >= %(start)s AND p.updated_at < %(end_excl)s")


def source_window(settings: Settings) -> Window:
    """Диапазон updated_at в источнике"""
    with connect(settings.source_db, autocommit=True) as src:
        return updated_at_range(src, sql.Identifier("prices"))


def run_extract(settings: Settings, window: Window) -> dict:
    """Справочники, затем сделки по месяцам. Каждая транзакция — своя запись в etl.run_log."""
    with connect(settings.source_db) as src, connect(settings.dwh_db) as dst:
        src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        src.read_only = True

        with step_run(settings.dwh_db, "extract_dictionaries") as run, src.transaction(), dst.transaction():
            lock(dst, "raw")
            run.stats.update(_reload_dictionaries(src, dst))
        log.info("extract.dictionaries.done", extra={"run_id": run.run_id, **run.stats})

        totals = {"windows": 0, "raw_prices": 0, "raw_price_body_types": 0, "raw_price_dates": 0}
        for month in month_windows(window):
            with step_run(settings.dwh_db, "extract_prices", month.start, month.end) as run, \
                 src.transaction(), dst.transaction():
                lock(dst, "raw")
                run.stats.update(_stage_changed_prices(src, dst, month))
                _replace_prices_in_raw(dst)
            log.info("extract.prices.done",
                     extra={"run_id": run.run_id, "start": month.start, "end": month.end, **run.stats})
            totals["windows"] += 1
            for key in ("raw_prices", "raw_price_body_types", "raw_price_dates"):
                totals[key] += run.stats[key]
    log.info("extract.done", extra={"start": window.start, "end": window.end, **totals})
    return totals


def _raw_columns(dst: psycopg.Connection, table: str) -> list[str]:
    """Столбцы источника для таблицы raw.<table> (без технических столбцов raw, начинающихся с «_»)."""
    rows = dst.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'raw' AND table_name = %s AND column_name NOT LIKE '\\_%%' "
        "ORDER BY ordinal_position", (table,)).fetchall()
    if not rows:
        raise RuntimeError(f"Table raw.{table} not found: run `pipeline init-db` first")
    return [r[0] for r in rows]


def _column_list(columns: list[str], alias: str | None = None) -> sql.Composable:
    return sql.SQL(", ").join(sql.Identifier(*((alias, c) if alias else (c,))) for c in columns)


def _copy(src: psycopg.Connection, dst: psycopg.Connection, query: sql.Composable, target: sql.Composable,
          columns: list[str], params: dict | None = None) -> int:
    """Потоковый COPY из источник в DWH: данные идут чанками, в памяти Python не накапливаются."""
    with src.cursor() as s_cur, dst.cursor() as d_cur:
        with s_cur.copy(sql.SQL("COPY ({}) TO STDOUT").format(query), params) as out, \
             d_cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(target, _column_list(columns))) as inp:
            for chunk in out:
                inp.write(chunk)
        return d_cur.rowcount


def _reload_dictionaries(src: psycopg.Connection, dst: psycopg.Connection) -> dict:
    """Справочники — полная перезагрузка (DELETE + COPY в транзакции: читатели видят старый снимок до коммита)."""
    stats = {}
    for table in DICTIONARIES:
        columns = _raw_columns(dst, table)
        target = sql.Identifier("raw", table)
        dst.execute(sql.SQL("DELETE FROM {}").format(target))
        query = sql.SQL("SELECT {} FROM {}").format(_column_list(columns), sql.Identifier(table))
        stats[f"raw_{table}"] = _copy(src, dst, query, target, columns)
    return stats


def _stage_changed_prices(src: psycopg.Connection, dst: psycopg.Connection, window: Window) -> dict:
    """Сделки и все их дочерние строки — во временные таблицы tmp_* (с ON COMMIT DROP)."""
    stats = {}
    for table in ("prices", *PRICE_CHILDREN):
        columns = _raw_columns(dst, table)
        tmp = sql.Identifier(f"tmp_{table}")
        dst.execute(sql.SQL("CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT {} FROM {} WITH NO DATA")
                    .format(tmp, _column_list(columns), sql.Identifier("raw", table)))
        if table == "prices":
            query = sql.SQL("SELECT {} FROM prices p WHERE {}").format(_column_list(columns, "p"), CHANGED_IN_WINDOW)
        else:
            query = sql.SQL("SELECT {} FROM {} c JOIN prices p ON p.id = c.price_id WHERE {}").format(
                _column_list(columns, "c"), sql.Identifier(table), CHANGED_IN_WINDOW)
        stats[f"raw_{table}"] = _copy(src, dst, query, tmp, columns, window.params)
    return stats


def _replace_prices_in_raw(dst: psycopg.Connection) -> None:
    """Перезапись по ключу: старые версии сделок окна и весь набор их дочерних строк заменяются новыми."""
    for child in PRICE_CHILDREN:
        dst.execute(sql.SQL("DELETE FROM {} WHERE price_id IN (SELECT id FROM tmp_prices)")
                    .format(sql.Identifier("raw", child)))
    dst.execute("DELETE FROM raw.prices WHERE id IN (SELECT id FROM tmp_prices)")

    for table in ("prices", *PRICE_CHILDREN):
        columns = _column_list(_raw_columns(dst, table))
        dst.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(
            sql.Identifier("raw", table), columns, columns, sql.Identifier(f"tmp_{table}")))
