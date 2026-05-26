from __future__ import annotations

from datetime import UTC, datetime

from app.utils.cron import next_cron_time


def test_next_cron_time_preserves_timezone_for_naive_cron_result(monkeypatch):
    base_time = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    naive_next_time = datetime(2026, 4, 29, 12, 5)

    class FakeIterator:
        def get_next(self, _type):
            return naive_next_time

    monkeypatch.setattr("app.utils.cron.croniter", lambda expression, base_time: FakeIterator())

    result = next_cron_time("* * * * *", base_time)

    assert result == naive_next_time.replace(tzinfo=UTC)
    assert result.tzinfo == UTC
