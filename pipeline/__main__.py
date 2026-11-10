"""CLI — точка входа

Примеры:
    python -m pipeline init-db
    python -m pipeline extract --start 2026-09-01 --end 2026-09-30     # источник -> raw
    python -m pipeline transform --start 2026-09-01 --end 2026-09-30   # raw -> core + карантин
    python -m pipeline etl --start 2026-09-01 --end 2026-09-30         # extract + transform за одно окно
    python -m pipeline marts         # инкремент: последние N месяцев (MART_LOOKBACK_MONTHS) и пропущенные
    python -m pipeline marts --full  # все месяцы
    python -m pipeline ml run        # train + score
    python -m pipeline run-all       # всё целиком

    python -m pipeline clickhouse-init   # под администратором ClickHouse
    python -m pipeline clickhouse-sync --start 2026-10-26 --end 2026-10-26 # инкремент за окно updated_at
    python -m pipeline clickhouse-sync   # без окна — полная синхронизация

Дата снимка as_of (marts, ml, run-all): --as-of > AS_OF из окружения > последний день данных
(для run-all — конец окна загрузки, для marts / ml — максимальный updated_at в raw).
"""

import argparse
import sys
from datetime import date

from common.config import Settings
from common.log import get_logger, setup_logging

log = get_logger("pipeline")


def _date(value: str) -> date:
    return date.fromisoformat(value)


def cmd_init_db(settings: Settings, args) -> None:
    from etl.migrate import init_db
    init_db(settings)


def cmd_extract(settings: Settings, args) -> None:
    from etl.extract import run_extract, source_window
    from etl.window import resolve_window
    run_extract(settings, resolve_window(args.start, args.end, lambda: source_window(settings)))


def cmd_transform(settings: Settings, args) -> None:
    from etl.transform import raw_window, run_transform
    from etl.window import resolve_window
    run_transform(settings, resolve_window(args.start, args.end, lambda: raw_window(settings)))


def cmd_etl(settings: Settings, args) -> None:
    from etl.extract import run_extract, source_window
    from etl.transform import run_transform
    from etl.window import resolve_window
    window = resolve_window(args.start, args.end, lambda: source_window(settings))
    run_extract(settings, window)
    run_transform(settings, window)


def _as_of(settings: Settings, args) -> date:
    """as_of для marts / ml: --as-of > AS_OF > последний день данных в raw."""
    from etl.transform import raw_window
    from etl.window import resolve_as_of
    return resolve_as_of(args.as_of, settings.as_of, lambda: raw_window(settings).end)


def cmd_marts(settings: Settings, args) -> None:
    from etl.marts import build_marts
    build_marts(settings, _as_of(settings, args), full=args.full)


def cmd_ml(settings: Settings, args) -> None:
    from ml.pipeline import score, train
    as_of = _as_of(settings, args)
    if args.action in ("train", "run"):
        train(settings, as_of)
    if args.action in ("score", "run"):
        score(settings, as_of)


def cmd_clickhouse_init(settings: Settings, args) -> None:
    from etl.clickhouse import init_clickhouse
    init_clickhouse(settings)


def cmd_clickhouse(settings: Settings, args) -> None:
    from etl.clickhouse import sync_clickhouse
    from etl.window import Window
    if bool(args.start) != bool(args.end):
        raise ValueError("clickhouse-sync: pass both --start and --end (window) or neither (full sync)")
    sync_clickhouse(settings, Window(args.start, args.end) if args.start else None)


def cmd_run_all(settings: Settings, args) -> None:
    from etl.extract import run_extract, source_window
    from etl.marts import build_marts
    from etl.migrate import init_db
    from etl.transform import run_transform
    from etl.window import resolve_as_of, resolve_window

    init_db(settings)
    window = resolve_window(args.start, args.end, lambda: source_window(settings))
    as_of = resolve_as_of(args.as_of, settings.as_of, lambda: window.end)
    log.info("run_all.window", extra={"start": window.start, "end": window.end, "as_of": as_of})
    run_extract(settings, window)
    run_transform(settings, window)
    build_marts(settings, as_of, full=True)

    from ml.pipeline import score, train
    train(settings, as_of)
    score(settings, as_of)
    log.info("run_all.done")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline", description="ul-platform: ETL -> DQ -> Витрины -> ML")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="применить DDL-миграции DWH").set_defaults(func=cmd_init_db)

    for name, func, help_text in (
        ("extract", cmd_extract, "источник -> raw: сделки окна по updated_at (по умолчанию — весь источник)"),
        ("transform", cmd_transform, "raw -> core + карантин: сделки окна по updated_at (по умолчанию — весь raw)"),
        ("etl", cmd_etl, "extract + transform за одно окно (по умолчанию — весь источник)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--start", type=_date, help="updated_at с (включительно)")
        p.add_argument("--end", type=_date, help="updated_at по (включительно)")
        p.set_defaults(func=func)

    as_of_help = "дата снимка (по умолчанию AS_OF, иначе последний день данных)"
    p = sub.add_parser("marts", help="пересчёт витрин из core (инкремент по месяцам)")
    p.add_argument("--as-of", type=_date, help=as_of_help)
    p.add_argument("--full", action="store_true", help="пересчитать все месяцы")
    p.set_defaults(func=cmd_marts)

    p = sub.add_parser("ml", help="обучение и скоринг")
    p.add_argument("action", choices=["train", "score", "run"])
    p.add_argument("--as-of", type=_date, help=as_of_help)
    p.set_defaults(func=cmd_ml)

    sub.add_parser("clickhouse-init", help="(бонус таска) DDL ClickHouse и ETL-пользователь (под администратором)") \
        .set_defaults(func=cmd_clickhouse_init)

    p = sub.add_parser("clickhouse-sync", help="(бонус таска) синхронизация ClickHouse из core (под ETL-пользователем)")
    p.add_argument("--start", type=_date, help="окно updated_at с (включительно); без окна — полная синхронизация")
    p.add_argument("--end", type=_date, help="окно updated_at по (включительно)")
    p.set_defaults(func=cmd_clickhouse)

    p = sub.add_parser("run-all", help="миграции + extract + transform + витрины + ML")
    p.add_argument("--start", type=_date)
    p.add_argument("--end", type=_date)
    p.add_argument("--as-of", type=_date, help="дата снимка (по умолчанию AS_OF, иначе конец окна)")
    p.set_defaults(func=cmd_run_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    try:
        settings = Settings.from_env()
        setup_logging(settings.log_level)
        args.func(settings, args)
    except Exception:
        log.exception("pipeline.failed", extra={"command": args.command})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
