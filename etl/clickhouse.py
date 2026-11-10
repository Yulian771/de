"""ClickHouse: загрузка рыночной выборки из core, расчёт витрины на MergeTree плюс сверка с PostgreSQL.

Рыночная выборка:
  * за окно [start, end] по updated_at — вставками
  * без окна — все месяцы с заменой партиций (03_load_base.sql)

Словарь городов перезагружается из core.dim_city.

Витрина повторяет пересчёты витрины PostgreSQL: пересчитываются месяцы, которые PostgreSQL пересчитал после
прошлой синхронизации (по моменту пересчёта месяца).

Две учётные записи:
  администратор (default)  — только: DDL и создание ETL-пользователя с правами;
  ETL-пользователь         — загрузка, витрина, сверка.
"""

import re
import time
from datetime import date, datetime, timezone
from string import Template

import numpy as np
import pandas as pd
import psycopg

from common.config import Settings
from common.db import connect, lock, step_run
from common.log import get_logger
from etl.window import Window

log = get_logger(__name__)

KEYS = ["month", "loading_city_gcid", "unloading_city_gcid", "body_group", "weight_category"]
EXACT_COLUMNS = ["trips_count", "carriers_count", "reliability_level", "freshness_days", "last_price_date", "as_of",
                 "loading_city", "loading_region", "unloading_city", "unloading_region"]
TEXT_COLUMNS = ["body_group", "weight_category", "reliability_level",
                "loading_city", "loading_region", "unloading_city", "unloading_region"]
NUMERIC_COLUMNS = ["p25_trip", "median_trip", "p75_trip", "avg_trip", "p25_km", "median_km", "p75_km",
                   "mom_pct", "yoy_pct"]
TOLERANCE = 0.011   # PostgreSQL хранит значения, округлённые до 0.01
REQUIRED_OBJECTS = ("market_base", "market_base_current", "dict_city", "mart_route_card")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ClickHouseClient:
    """HTTP-клиент clickhouse-connect"""

    def __init__(self, host: str, port: int, user: str, password: str):
        import clickhouse_connect
        self._client = clickhouse_connect.get_client(host=host, port=port, username=user, password=password)

    def command(self, sql: str) -> None:
        self._client.command(sql)

    def query_df(self, sql: str) -> pd.DataFrame:
        return self._client.query_df(sql)

    def run_script(self, sql: str) -> None:
        """HTTP-интерфейс принимает один оператор за запрос: режем файл по ";" в конце строки."""
        for statement in (s.strip() for s in sql.split(";\n")):
            body = "\n".join(line for line in statement.splitlines() if not line.strip().startswith("--")).strip()
            if body:
                self.command(body.rstrip(";"))


def etl_client(settings: Settings) -> ClickHouseClient:
    ch = settings.clickhouse()
    return ClickHouseClient(ch.host, ch.port, ch.user, ch.password)


def admin_client(settings: Settings) -> ClickHouseClient:
    ch = settings.clickhouse()
    if not ch.admin_password:
        raise RuntimeError("CLICKHOUSE_ADMIN_PASSWORD is required for clickhouse-init")
    return ClickHouseClient(ch.host, ch.port, ch.admin_user, ch.admin_password)


def _identifier(value: str, name: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"{name}={value!r} is not a valid ClickHouse identifier")
    return value


def _string_literal(value: str) -> str:
    """Экранирование для подстановки внутрь SQL ClickHouse."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _databases(settings: Settings) -> tuple[str, str]:
    """Целевая база и промежуточная (<целевая>_stage)."""
    db = _identifier(settings.clickhouse().database, "CLICKHOUSE_DATABASE")
    return db, f"{db}_stage"


def _render(settings: Settings, name: str, **extra) -> str:
    db, stage_db = _databases(settings)
    values = {"db": db, "stage_db": stage_db, **extra}
    return Template((settings.sql_dir / "clickhouse" / name).read_text(encoding="utf-8")).substitute(values)


def init_clickhouse(settings: Settings) -> dict:
    """Под администратором: база и таблицы (01_ddl.sql), ETL-пользователь и его права (02_access.sql)"""
    ch = settings.clickhouse()
    client = admin_client(settings)
    client.run_script(_render(settings, "01_ddl.sql"))
    client.run_script(_render(settings, "02_access.sql",
                              etl_user=_identifier(ch.user, "CLICKHOUSE_USER"),
                              etl_password=_string_literal(ch.password)))
    stats = {"database": ch.database, "stage_database": _databases(settings)[1], "etl_user": ch.user}
    log.info("clickhouse.init.done", extra=stats)
    return stats


def sync_clickhouse(settings: Settings, window: Window | None = None) -> dict:
    """Под ETL-пользователем: рыночная выборка (за окно или полностью), словарь, витрина, сверка с PG."""
    db, stage_db = _databases(settings)
    full = window is None
    with step_run(settings.dwh_db, "clickhouse_sync", None if full else window.start,
                  None if full else window.end) as run, connect(settings.dwh_db) as pg, pg.transaction():
        lock(pg, "core", shared=True)
        lock(pg, "mart", shared=True)
        lock(pg, "clickhouse")

        client = etl_client(settings)
        existing = set(client.query_df(f"SELECT name FROM system.tables WHERE database = '{db}'")["name"])
        missing = [t for t in REQUIRED_OBJECTS if t not in existing]
        if missing:
            raise RuntimeError(f"ClickHouse objects {missing} not found in {db}: run `pipeline clickhouse-init` first")
        _drop_stage_tables(client, stage_db) # остатки упавших запусков

        tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        try:
            base = _sync_market_base(settings, client, window, f"{stage_db}.market_base_{tag}")
            client.command(f"SYSTEM RELOAD DICTIONARY {db}.dict_city")
            plan = _mart_plan(pg, client, db, full)
            mart = _sync_mart(settings, client, plan, f"{stage_db}.mart_route_card_{tag}")
        finally:
            _drop_stage_tables(client, stage_db, tag)

        counts = client.query_df(
            f"SELECT (SELECT count() FROM {db}.market_base_current) AS base_rows, "
            f"(SELECT count() FROM {db}.dict_city) AS cities").iloc[0]
        run.stats.update({
            "full": full, **base, **mart,
            "market_base_rows": int(counts["base_rows"]), "dict_city_rows": int(counts["cities"]),
            "as_of": max(plan["as_of"].values()),
        })
        run.stats.update(reconcile(settings, client, pg))
    log.info("clickhouse.sync.done", extra={"run_id": run.run_id, **run.stats})
    return run.stats


def _sync_market_base(settings: Settings, client: ClickHouseClient, window: Window | None, stage: str) -> dict:
    """Окно — вставками версий (только изменённые сделки); без окна — все месяцы заменой партиций."""
    db, _ = _databases(settings)
    version = time.time_ns() // 1000
    if window is not None:
        client.run_script(_render(settings, "03_load_base_window.sql", version=version,
                                  start=window.start.isoformat(), end_excl=window.end_excl.isoformat()))
        return {"base_mode": "window"}

    client.command(f"CREATE TABLE {stage} AS {db}.market_base")
    client.run_script(_render(settings, "03_load_base.sql", stage_table=stage, version=version))
    loaded = _partitions(client, stage)
    target = _partitions(client, f"{db}.market_base")
    to_drop = target - loaded
    _replace_partitions(client, f"{db}.market_base", stage, loaded, to_drop)
    return {"base_mode": "full", "base_months_replaced": len(loaded), "base_months_dropped": len(to_drop)}


def _sync_mart(settings: Settings, client: ClickHouseClient, plan: dict, stage: str) -> dict:
    """Месяцы витрины, пересчитанные в PostgreSQL — в промежуточную таблицу, затем в целевую с заменой партиций."""
    db, _ = _databases(settings)
    target = _partitions(client, f"{db}.mart_route_card")
    loaded: set[str] = set()
    if plan["recompute"]:
        client.command(f"CREATE TABLE {stage} AS {db}.mart_route_card")
        client.run_script(_render(settings, "04_mart.sql", stage_table=stage, **_mart_params(plan)))
        loaded = _partitions(client, stage)
    recomputed = {_partition_id(m) for m in plan["recompute"]}
    in_pg = {_partition_id(m) for m in plan["as_of"]}
    to_drop = (recomputed - loaded) | (target - in_pg)
    _replace_partitions(client, f"{db}.mart_route_card", stage, loaded, to_drop)
    return {"months_recomputed": len(plan["recompute"]), "months_kept": len(plan["keep"]),
            "mart_months_dropped": len(to_drop & target)}


def _partition_id(month: date) -> str:
    return f"{month:%Y%m}"


def _partitions(client: ClickHouseClient, table: str) -> set[str]:
    """Партиции таблицы с данными"""
    df = client.query_df(f"SELECT DISTINCT _partition_id AS partition_id FROM {table}")
    return set() if df.empty else set(df["partition_id"].astype(str))


def _replace_partitions(client: ClickHouseClient, target: str, stage: str, replace: set[str], drop: set[str]) -> None:
    for partition in sorted(replace):
        client.command(f"ALTER TABLE {target} REPLACE PARTITION ID '{partition}' FROM {stage}")
    for partition in sorted(drop):
        client.command(f"ALTER TABLE {target} DROP PARTITION ID '{partition}'")


def _drop_stage_tables(client: ClickHouseClient, stage_db: str, tag: str | None = None) -> None:
    """Удаление промежуточных таблиц запуска"""
    df = client.query_df(f"SELECT name FROM system.tables WHERE database = '{stage_db}'")
    for name in ([] if df.empty else df["name"]):
        if tag is None or name.endswith(f"_{tag}"):
            client.command(f"DROP TABLE IF EXISTS {stage_db}.{name}")


def _mart_plan(pg: psycopg.Connection, client: ClickHouseClient, db: str, full: bool) -> dict:
    """Какие месяцы витрины пересчитать: пересчитанные в PostgreSQL после прошлой синхронизации (или все)."""
    pg_months = {m: (as_of, _utc(updated)) for m, as_of, updated in pg.execute(
        "SELECT month, max(as_of), max(updated_at) FROM mart.mart_route_card GROUP BY month ORDER BY month")}
    if not pg_months:
        raise RuntimeError("PostgreSQL mart is empty: run `pipeline marts` first")
    ch_df = client.query_df(f"SELECT month, max(pg_updated_at) AS refreshed FROM {db}.mart_route_card GROUP BY month")
    ch_months = {} if ch_df.empty else {
        pd.Timestamp(m).date(): _utc(r) for m, r in zip(ch_df["month"], ch_df["refreshed"])}
    recompute = [m for m, (_, refreshed) in pg_months.items() if full or ch_months.get(m) != refreshed]
    keep = [m for m in pg_months if m not in recompute and m in ch_months]
    return {"recompute": recompute, "keep": keep,
            "as_of": {m: pg_months[m][0] for m in pg_months},
            "refreshed": {m: pg_months[m][1] for m in recompute}}


def _mart_params(plan: dict) -> dict:
    def dates(values) -> str:
        return "CAST([" + ", ".join(f"'{v.isoformat()}'" for v in values) + "] AS Array(Date))"
    recompute = plan["recompute"]
    return {
        "months": dates(recompute),
        "as_ofs": dates(plan["as_of"][m] for m in recompute),
        "refreshed": "CAST([" + ", ".join(f"'{plan['refreshed'][m]}'" for m in recompute)
                     + "] AS Array(DateTime64(6, 'UTC')))",
        "keep_months": dates(plan["keep"]),
    }


def _utc(value) -> str:
    """Момент пересчёта месяца как строка UTC с микросекундами — одинаково для PostgreSQL и ClickHouse."""
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")


def reconcile(settings: Settings, client: ClickHouseClient | None = None,
              pg: psycopg.Connection | None = None) -> dict:
    """Сверка витрины ClickHouse с PostgreSQL по каждой ячейке. Расхождение -> исключение."""
    client = client or etl_client(settings)
    cols = KEYS + EXACT_COLUMNS + NUMERIC_COLUMNS
    ch = client.query_df(f"SELECT {', '.join(cols)} FROM {settings.clickhouse().database}.mart_route_card")
    if pg is None:
        with connect(settings.dwh_db, autocommit=True) as own:
            pg_df = _pg_mart(own, cols)
    else:
        pg_df = _pg_mart(pg, cols)

    for df in (ch, pg_df):
        df["month"] = pd.to_datetime(df["month"]).dt.date
        df["last_price_date"] = pd.to_datetime(df["last_price_date"]).dt.date
        df["as_of"] = pd.to_datetime(df["as_of"]).dt.date
        for col in ("loading_city_gcid", "unloading_city_gcid", "trips_count", "carriers_count", "freshness_days"):
            df[col] = df[col].astype("int64")
        for col in NUMERIC_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        for col in TEXT_COLUMNS:
            df[col] = df[col].astype(object).fillna("").astype(str)

    merged = pg_df.merge(ch, on=KEYS, how="outer", suffixes=("_pg", "_ch"), indicator=True)
    only_one_side = int((merged["_merge"] != "both").sum())
    both = merged[merged["_merge"] == "both"]
    mismatches: dict[str, int] = {}
    for col in EXACT_COLUMNS:
        mismatches[col] = int((both[f"{col}_pg"] != both[f"{col}_ch"]).sum())
    for col in NUMERIC_COLUMNS:
        a, b = both[f"{col}_pg"].to_numpy(), both[f"{col}_ch"].to_numpy()
        nan_mismatch = np.isnan(a) != np.isnan(b)
        value_mismatch = ~np.isnan(a) & ~np.isnan(b) & (np.abs(a - b) > TOLERANCE)
        mismatches[col] = int((nan_mismatch | value_mismatch).sum())

    result = {"cells_pg": len(pg_df), "cells_ch": len(ch), "cells_only_one_side": only_one_side,
              "mismatches": {k: v for k, v in mismatches.items() if v}}
    if only_one_side or result["mismatches"]:
        raise RuntimeError(f"ClickHouse vs PostgreSQL reconciliation failed: {result}")
    log.info("clickhouse.reconcile.ok", extra=result)
    return result


def _pg_mart(conn: psycopg.Connection, cols: list[str]) -> pd.DataFrame:
    cur = conn.execute(f"SELECT {', '.join(cols)} FROM mart.mart_route_card")
    return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
