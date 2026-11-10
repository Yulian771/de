"""Окна загрузки по prices.updated_at

Каждый шаг ETL — идемпотентная задача на окно [start, end] без хранимого состояния:
extract и transform за одно и то же окно обрабатывают одни и те же сделки, повторный запуск
даёт тот же результат. Период длиннее месяца (backfill) делится на календарные месяцы —
по транзакции на месяц, сбой откатывает только текущий месяц.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import psycopg
from psycopg import sql

from common.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Window:
    """Окно [start, end] по дням; в запросах — полуинтервал [start 00:00, end+1 00:00)."""
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"start_date {self.start} > end_date {self.end}")

    @property
    def end_excl(self) -> date:
        return self.end + timedelta(days=1)

    @property
    def params(self) -> dict:
        return {"start": self.start, "end_excl": self.end_excl}


def month_windows(window: Window) -> list[Window]:
    """Делит окно на календарные месяцы: [start..конец месяца], [1-е..конец]."""
    windows = []
    current = window.start
    while current <= window.end:
        next_month = (current.replace(day=1) + timedelta(days=32)).replace(day=1)
        windows.append(Window(current, min(window.end, next_month - timedelta(days=1))))
        current = next_month
    return windows


def updated_at_range(conn: psycopg.Connection, table: sql.Composable) -> Window:
    """Весь диапазон updated_at в таблице сделок — окно по умолчанию (полная загрузка)."""
    first, last, no_mark = conn.execute(sql.SQL(
        "SELECT min(updated_at)::date, max(updated_at)::date, count(*) FILTER (WHERE updated_at IS NULL) "
        "FROM {}").format(table)).fetchone()
    if first is None:
        raise RuntimeError(f"Table {table.as_string(conn)} has no rows with updated_at")
    if no_mark:
        log.warning("window.updated_at_null", extra={"table": table.as_string(conn), "rows": no_mark})
    return Window(first, last)


def resolve_as_of(explicit: date | None, configured: date | None, last_data_day: date) -> date:
    """Дата снимка as_of (сделки после неё — будущее: не входят в витрину, для ML — заявки на скоринг).

    Порядок: явная (CLI --as-of, параметр DAG) > настроенная (AS_OF) > последний день данных.
    Снимок не может быть позже последнего дня данных (последнего updated_at): иначе плановые заявки
    до as_of считались бы фактом, а будущие месяцы попали бы в витрину.
    """
    as_of = explicit or configured or last_data_day
    if as_of > last_data_day:
        raise ValueError(f"as_of {as_of} is later than the last day of data {last_data_day}: "
                         "a snapshot cannot be taken after the data it is built on")
    return as_of


def resolve_window(start: date | None, end: date | None, full: Callable[[], Window]) -> Window:
    """Окно из параметров; недостающая граница берётся из full() (диапазон updated_at таблицы)."""
    if start and end:
        return Window(start, end)
    default = full()
    return Window(start or default.start, end or default.end)
