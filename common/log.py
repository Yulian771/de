"""Структурированное логирование: событие + поля"""

import json
import logging
import sys
from collections.abc import Mapping, MutableMapping
from datetime import date, datetime, timezone
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "event"}


def json_default(value: object) -> object:
    """Сериализация значений, которых нет в JSON"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _logfmt(fields: Mapping[str, Any]) -> str:
    """Поля одной строкой key=value; вложенные словари и списки — JSON."""
    def value(v: Any) -> str:
        if isinstance(v, (dict, list, tuple)):
            return json.dumps(v, ensure_ascii=False, default=json_default, separators=(",", ":"))
        return json_default(v) if isinstance(v, (date, datetime)) else str(v)
    return " ".join(f"{k}={value(v)}" for k, v in fields.items())


class EventLogger(logging.LoggerAdapter):
    """Логгер событий"""

    def process(self, msg: Any, kwargs: MutableMapping[str, Any]) -> tuple[Any, MutableMapping[str, Any]]:
        fields = dict(kwargs.get("extra") or {})
        kwargs["extra"] = {**fields, "event": msg}
        return (f"{msg} {_logfmt(fields)}" if fields else msg), kwargs


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=json_default)


def setup_logging(level: str = "INFO") -> None:
    """JSON-логи в stdout для процесса CLI"""
    root = logging.getLogger()
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name), {})
