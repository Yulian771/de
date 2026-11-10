"""Хронологическое разбиение относительно фиксированной даты снимка as_of (не системной даты)."""

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd


@dataclass(frozen=True)
class TimeSplit:
    as_of: date
    train_start: date   # включительно
    test_start: date    # граница: только в test
    val_start: date     # хвост train для early stopping (внутри train, тоже по времени)

    @classmethod
    def from_as_of(cls, as_of: date, val_days: int = 14) -> "TimeSplit":
        train_start = (pd.Timestamp(as_of) - pd.DateOffset(months=3)).date()   # as_of - 3 месяца
        test_start = as_of - timedelta(weeks=3)                                # as_of - 3 недели
        return cls(as_of=as_of, train_start=train_start, test_start=test_start,
                   val_start=test_start - timedelta(days=val_days))

    def masks(self, loading_date: pd.Series) -> dict[str, pd.Series]:
        d = pd.to_datetime(loading_date).dt.date
        return {
            "train": (d >= self.train_start) & (d < self.test_start),
            "test": (d >= self.test_start) & (d <= self.as_of),
            "future": d > self.as_of,
        }

    def require_as_of(self, as_of: date) -> None:
        """Скоринг — на ту же дату снимка, на которую модель обучена: test — её отложенный период,
        future — заявки после as_of. На другую дату модель нужно переобучить."""
        if as_of != self.as_of:
            raise ValueError(f"model is trained for as_of {self.as_of}, scoring requested for {as_of}: "
                             f"run `ml train --as-of {as_of}` first")
