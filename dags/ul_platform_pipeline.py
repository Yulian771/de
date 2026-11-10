"""Airflow 3: тонкая обёртка над пакетом проекта."""

from datetime import date, datetime, timedelta

from airflow.sdk import Param, chain, dag, get_current_context, task

# Все шаги идемпотентны — повтор безопасен
DEFAULT_ARGS = {"retries": 2, "retry_delay": timedelta(minutes=1), "execution_timeout": timedelta(hours=1)}


def _settings():
    from common.config import Settings
    return Settings.from_env()


def _param_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _window(window: dict):
    from etl.window import Window
    return Window(date.fromisoformat(window["start"]), date.fromisoformat(window["end"]))


@task
def resolve_window(use_data_interval: bool) -> dict:
    """Окно запуска (params, иначе data interval для daily, иначе весь источник) и дата снимка as_of."""
    from etl.extract import source_window
    from etl.window import resolve_as_of
    from etl.window import resolve_window as _resolve_window

    ctx = get_current_context()
    params = ctx["params"]
    start = params.get("start_date")
    end = params.get("end_date")
    if not (start and end) and use_data_interval and ctx.get("data_interval_start"):
        interval_start = ctx["data_interval_start"].date()
        interval_end = (ctx["data_interval_end"] - timedelta(days=1)).date()
        start = min(interval_start, interval_end).isoformat()
        end = interval_end.isoformat()
    settings = _settings()
    data_end = source_window(settings).end                  # последний updated_at в источнике
    window = _resolve_window(_param_date(start), _param_date(end), lambda: source_window(settings))
    as_of = resolve_as_of(_param_date(params.get("as_of")), settings.as_of, data_end)
    return {"start": window.start.isoformat(), "end": window.end.isoformat(), "as_of": as_of.isoformat()}


@task
def init_db() -> None:
    from etl.migrate import init_db as _init_db
    _init_db(_settings())


@task
def extract(window: dict) -> dict:
    from etl.extract import run_extract
    return run_extract(_settings(), _window(window))


@task
def transform(window: dict) -> dict:
    from etl.transform import run_transform
    return run_transform(_settings(), _window(window))


@task
def marts(window: dict, incremental: bool = False) -> dict:
    from etl.marts import build_marts
    return build_marts(_settings(), date.fromisoformat(window["as_of"]), incremental=incremental)


@task
def ml_train(window: dict) -> dict:
    from ml.pipeline import train
    report = train(_settings(), date.fromisoformat(window["as_of"]))
    return {"model_version": report["model_version"],
            **{name: m["metrics"] for name, m in report["models"].items()}}


@task
def ml_score(window: dict) -> dict:
    from ml.pipeline import score
    return score(_settings(), date.fromisoformat(window["as_of"]))


@task
def clickhouse_sync() -> dict:
    from etl.clickhouse import sync_clickhouse
    from etl.window import Window
    params = get_current_context()["params"]
    start, end = _param_date(params.get("start_date")), _param_date(params.get("end_date"))
    if bool(start) != bool(end):
        raise ValueError("clickhouse_sync: set both start_date and end_date (window) or neither (full sync)")
    return sync_clickhouse(_settings(), Window(start, end) if start else None)


WINDOW_PARAMS = {
    "start_date": Param(None, type=["null", "string"], format="date", description="дата с (включительно)"),
    "end_date": Param(None, type=["null", "string"], format="date", description="дата по (включительно)"),
    "as_of": Param(None, type=["null", "string"], format="date", description="дата снимка (по умолчанию AS_OF, иначе последний день данных)"),
}


@dag(
    dag_id="ul_platform_backfill",
    description="Полная загрузка за окно: миграции, extract, transform, полный пересчёт витрины, обучение и скоринг",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args=DEFAULT_ARGS,
    params=WINDOW_PARAMS,
    tags=["backfill", "etl", "ml"],
    doc_md=__doc__,
)
def ul_platform_backfill():
    window = resolve_window(use_data_interval=False)
    chain(init_db(), window, extract(window), transform(window), marts(window),
          ml_train(window), ml_score(window))


@dag(
    dag_id="ul_platform_daily",
    description="Инкремент за день: extract, transform, инкремент витрины, обучение и скоринг на дату снимка",
    schedule="@daily",
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args=DEFAULT_ARGS,
    params=WINDOW_PARAMS,
    tags=["daily", "etl", "ml"],
    doc_md=__doc__,
)
def ul_platform_daily():
    window = resolve_window(use_data_interval=True)
    chain(window, extract(window), transform(window), marts(window, incremental=True),
          ml_train(window), ml_score(window))


@dag(
    dag_id="ul_platform_marts_full",
    description="Еженедельный полный пересчёт витрины: подбирает правки месяцев глубже окна инкремента",
    schedule="@weekly",
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args=DEFAULT_ARGS,
    params={"as_of": WINDOW_PARAMS["as_of"]},
    tags=["weekly", "marts"],
    doc_md=__doc__,
)
def ul_platform_marts_full():
    window = resolve_window(use_data_interval=True)
    chain(window, marts(window))


@dag(
    dag_id="ul_platform_clickhouse_sync",
    description="Синхронизация ClickHouse из core: за окно params (инкремент) или полностью без окна",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args=DEFAULT_ARGS,
    params={"start_date": WINDOW_PARAMS["start_date"], "end_date": WINDOW_PARAMS["end_date"]},
    tags=["clickhouse", "bonus"],
    doc_md=__doc__,
)
def ul_platform_clickhouse_sync():
    clickhouse_sync()


ul_platform_backfill()
ul_platform_daily()
ul_platform_marts_full()
ul_platform_clickhouse_sync()
