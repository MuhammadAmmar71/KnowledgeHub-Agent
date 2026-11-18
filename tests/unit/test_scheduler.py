from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db.models import SyncRunType
from app.jobs import LibrarySyncMode, choose_library_sync_type, reconciliation_due
from app.scheduler import next_run_after, parse_schedule_time


def test_next_run_is_later_today_when_time_not_yet_reached():
    assert next_run_after(datetime(2026, 10, 5, 1, 30), time(2, 0)) == datetime(2026, 10, 5, 2, 0)


def test_next_run_is_tomorrow_once_time_has_passed_or_is_now():
    assert next_run_after(datetime(2026, 10, 5, 2, 0), time(2, 0)) == datetime(2026, 10, 6, 2, 0)
    assert next_run_after(datetime(2026, 10, 5, 23, 59), time(2, 0)) == datetime(2026, 10, 6, 2, 0)


def test_next_run_keeps_wall_clock_time_across_dst_change():
    berlin = ZoneInfo("Europe/Berlin")
    # Clocks go back on 2026-10-25 (CEST -> CET); the run must stay at 04:00 local.
    now = datetime(2026, 10, 24, 12, 0, tzinfo=berlin)
    target = next_run_after(now, time(4, 0))
    assert target == datetime(2026, 10, 25, 4, 0, tzinfo=berlin)
    assert target.utcoffset() == timedelta(hours=1)


def test_parse_schedule_time():
    assert parse_schedule_time("02:00") == time(2, 0)
    assert parse_schedule_time("23:45") == time(23, 45)


def test_reconciliation_due():
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert reconciliation_due(None, now, 7) is True
    assert reconciliation_due(now - timedelta(days=7), now, 7) is True
    assert reconciliation_due(now - timedelta(days=6), now, 7) is False
    assert reconciliation_due(None, now, 0) is False  # disabled


def test_choose_library_sync_type():
    # No cursor yet: always the full walk that establishes one.
    for mode in LibrarySyncMode:
        assert choose_library_sync_type(mode, has_delta_cursor=False) == SyncRunType.initial
    assert choose_library_sync_type(LibrarySyncMode.auto, True) == SyncRunType.delta
    assert choose_library_sync_type(LibrarySyncMode.full, True) == SyncRunType.reconciliation
    assert choose_library_sync_type(LibrarySyncMode.reconcile, True) == SyncRunType.reconciliation
