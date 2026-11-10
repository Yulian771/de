"""Инициализация схемы DWH: идемпотентные DDL-миграции из sql/migrations."""

from common.config import Settings
from common.db import connect, lock, run_sql_dir, wait_for_db
from common.log import get_logger

log = get_logger(__name__)


def init_db(settings: Settings) -> None:
    wait_for_db(settings.dwh_db)
    with connect(settings.dwh_db) as conn:
        with conn.transaction():
            lock(conn, "migrations")
            run_sql_dir(conn, settings.sql_dir / "migrations")
    log.info("migrations.done")
