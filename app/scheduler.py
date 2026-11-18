"""
Daily sync scheduler (spec section 58): once a day at SYNC_SCHEDULE_TIME,
run JobRunner.run_scheduled() -- a delta sync, or a full reconciliation
walk when the last one is older than RECONCILIATION_INTERVAL_DAYS, then
pages, then embeddings.

A plain asyncio task inside the API process rather than an external cron:
the job locks (app/sync/locks.py) already guarantee that several instances
each running this scheduler still produce one sync, not several -- the
losers record a "failed: already running" run and move on.

A run missed because the service was down at the scheduled time is not
caught up; the next day's run picks up all changes anyway (delta is
cumulative), and POST /sync is always available.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from app.jobs import JobRunner

logger = logging.getLogger("knowledge_agent.scheduler")

# Sleep in slices no longer than this, re-reading the clock each time, so a
# suspended host or a clock adjustment can't make the scheduler oversleep.
_MAX_SLEEP_SECONDS = 300.0


def parse_schedule_time(hhmm: str) -> time:
    hour, minute = hhmm.split(":")
    return time(int(hour), int(minute))


def next_run_after(now: datetime, at: time) -> datetime:
    """
    The first moment strictly after `now` whose wall-clock time is `at`, in
    now's own timezone (naive now = server local time). Adding a day to an
    aware ZoneInfo datetime keeps the wall-clock time, so 02:00 stays 02:00
    across DST changes.
    """
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


def _seconds_until(target: datetime, now: datetime) -> float:
    if target.tzinfo is not None:
        return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    return (target - now).total_seconds()


class DailyScheduler:
    def __init__(self, runner: JobRunner, schedule_time: str, timezone_name: str = ""):
        self._runner = runner
        self._at = parse_schedule_time(schedule_time)
        self._tz: tzinfo | None = ZoneInfo(timezone_name) if timezone_name else None
        self._task: asyncio.Task | None = None

    def _now(self) -> datetime:
        return datetime.now(self._tz) if self._tz is not None else datetime.now()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="daily-sync-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            target = next_run_after(self._now(), self._at)
            logger.info("scheduler_next_run at=%s", target.isoformat())
            while (remaining := _seconds_until(target, self._now())) > 0:
                await asyncio.sleep(min(remaining, _MAX_SLEEP_SECONDS))
            try:
                await self._runner.run_scheduled()
            except Exception:  # noqa: BLE001 -- one bad night must not kill the scheduler
                logger.exception("scheduled_sync_crashed")
