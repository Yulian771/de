"""Хронологическое разбиение относительно фиксированной даты снимка as_of"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
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

    def scoring_labels(self, loading_date: pd.Series, scoring_as_of: date) -> pd.Series:
        """Разметка сделок для скоринга моделью, обученной на этом разбиении:

          test   — отложенный период модели [test_start, as_of] (на нём считались метрики обучения);
          live   — после обучения, факт уже известен: (as_of, scoring_as_of] — точность на новых данных;
          future — плановые заявки после даты скоринга (loading_date > scoring_as_of), факт не известен.
        Период train не скорится (None).
        """
        if scoring_as_of < self.as_of:
            raise ValueError(f"scoring as_of {scoring_as_of} is earlier than model as_of {self.as_of}")
        d = pd.to_datetime(loading_date).dt.date
        labels = np.select(
            [(d >= self.test_start) & (d <= self.as_of), (d > self.as_of) & (d <= scoring_as_of), d > scoring_as_of],
            ["test", "live", "future"], default=None)
        return pd.Series(labels, index=loading_date.index, dtype=object)
