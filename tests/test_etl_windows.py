from datetime import date

import pytest

from etl.window import Window, month_windows, resolve_as_of, resolve_window


def test_full_history_is_split_by_calendar_months():
    windows = month_windows(Window(date(2023, 9, 22), date(2026, 10, 26)))
    assert windows[0] == Window(date(2023, 9, 22), date(2023, 9, 30))
    assert windows[1] == Window(date(2023, 10, 1), date(2023, 10, 31))
    assert windows[-1] == Window(date(2026, 10, 1), date(2026, 10, 26))
    assert len(windows) == 38


def test_windows_cover_period_without_gaps_or_overlaps():
    windows = month_windows(Window(date(2024, 1, 31), date(2024, 3, 1)))
    assert windows == [Window(date(2024, 1, 31), date(2024, 1, 31)), Window(date(2024, 2, 1), date(2024, 2, 29)),
                       Window(date(2024, 3, 1), date(2024, 3, 1))]
    for prev, nxt in zip(windows, windows[1:]):
        assert nxt.start == prev.end_excl


def test_one_day_is_one_window():
    day = Window(date(2026, 10, 25), date(2026, 10, 25))
    assert month_windows(day) == [day]
    assert day.params == {"start": date(2026, 10, 25), "end_excl": date(2026, 10, 26)}


def test_reversed_period_is_rejected():
    with pytest.raises(ValueError):
        Window(date(2026, 10, 2), date(2026, 10, 1))


def test_missing_bound_is_taken_from_full_range():
    full = Window(date(2023, 9, 22), date(2026, 10, 26))
    assert resolve_window(date(2026, 9, 1), None, lambda: full) == Window(date(2026, 9, 1), date(2026, 10, 26))
    assert resolve_window(None, None, lambda: full) == full
    assert resolve_window(date(2026, 9, 1), date(2026, 9, 2), lambda: pytest.fail("not needed")) \
        == Window(date(2026, 9, 1), date(2026, 9, 2))


def test_as_of_priority_explicit_then_configured_then_data_end():
    data_end = lambda: date(2026, 10, 26)   # noqa: E731
    assert resolve_as_of(date(2026, 11, 1), date(2026, 10, 1), data_end) == date(2026, 11, 1)
    assert resolve_as_of(None, date(2026, 10, 1), data_end) == date(2026, 10, 1)
    assert resolve_as_of(None, None, data_end) == date(2026, 10, 26)
