"""Подключения к PostgreSQL, выполнение SQL-файлов."""

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any, Iterator, Mapping

import psycopg
from psycopg.types.json import Jsonb

from common.config import DbConfig
from common.log import get_logger, json_default

log = get_logger(__name__)

_json_dumps = partial(json.dumps, ensure_ascii=False, default=json_default)


def connect(cfg: DbConfig, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(cfg.conninfo, autocommit=autocommit)


def wait_for_db(cfg: DbConfig, timeout_s: int = 120) -> None:
    """Ждать готовности БД"""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with connect(cfg, autocommit=True) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError as exc:
            if time.monotonic() > deadline:
                raise
            log.warning("db.wait", extra={"host": cfg.host, "port": cfg.port, "dbname": cfg.dbname,
                                          "error": str(exc).strip()})
            time.sleep(2)


def set_params(conn: psycopg.Connection, params: Mapping[str, object]) -> None:
    """Параметры для SQL-файлов: set_config()"""
    for key, value in params.items():
        conn.execute("SELECT set_config(%s, %s, true)", (key, "" if value is None else str(value)))


def lock(conn: psycopg.Connection, name: str, shared: bool = False) -> None:
    """Advisory-блокировка до конца транзакции: ul_platform_<name>.

    Слои хранилища (raw, core, mart): шаг берёт эксклюзивную блокировку слоя, который пишет,
    и разделяемую — слоя, который читает (всегда в порядке raw -> core -> mart, чтобы не было взаимоблокировок).
    Так шаг не читает слой посреди чужой записи, а читатели одного слоя друг другу не мешают.
    """
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    conn.execute(f"SELECT {fn}(hashtext(%s))", (f"ul_platform_{name}",))


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    """Выполнить файл целиком"""
    started = time.monotonic()
    try:
        conn.execute(path.read_text(encoding="utf-8"))
    except psycopg.Error as exc:
        exc.add_note(f"while executing SQL file {path}")
        log.error("sql.file.failed", extra={"file": path.name, "error": str(exc).strip()})
        raise
    log.info("sql.file.done", extra={"file": path.name, "seconds": round(time.monotonic() - started, 2)})


def run_sql_dir(conn: psycopg.Connection, directory: Path) -> None:
    """Выполнить *.sql каталога по порядку имён (10_..., 20_...)."""
    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise FileNotFoundError(f"No SQL files in {directory}")
    for path in paths:
        run_sql_file(conn, path)


@dataclass
class StepRun:
    """Запуск шага пайплайна: run_id записи в etl.run_log и статистика, которая туда же пишется."""
    run_id: int
    step: str
    stats: dict[str, Any] = field(default_factory=dict)


@contextmanager
def step_run(cfg: DbConfig, step: str, start: date | None = None, end: date | None = None) -> Iterator[StepRun]:
    """Запись запуска шага в etl.run_log"""
    with connect(cfg, autocommit=True) as log_conn:
        run_id = log_conn.execute(
            "INSERT INTO etl.run_log (step, window_start, window_end) VALUES (%s, %s, %s) RETURNING run_id",
            (step, start, end),
        ).fetchone()[0]
        run = StepRun(run_id=run_id, step=step)
        status = "failed"
        try:
            yield run
            status = "success"
        finally:
            log_conn.execute(
                "UPDATE etl.run_log SET status = %s, finished_at = now(), stats = %s WHERE run_id = %s",
                (status, Jsonb(run.stats, dumps=_json_dumps), run_id),
            )
