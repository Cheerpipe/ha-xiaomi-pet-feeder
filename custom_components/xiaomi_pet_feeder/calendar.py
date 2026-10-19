"""Calendar: the feeding plan ahead and the feeding log behind (90 days)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import FeedRecord, FeedResult, ScheduleEntry, XiaomiError, device_now
from .api.schedule import GRAMS_PER_PORTION
from .const import DOMAIN
from .data import Feeder, XiaomiPetFeederConfigEntry
from .entity import FeederStatusEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
HISTORY_DAYS = 90
EVENT_LENGTH = timedelta(minutes=1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaomiPetFeederConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the calendar."""
    async_add_entities(FeedingCalendar(f) for f in entry.runtime_data.feeders.values())


class FeedingCalendar(FeederStatusEntity, CalendarEntity):
    """Planned feedings (future) and recorded meals/dispenses (past)."""

    _attr_translation_key = "feeding"

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "feeding_calendar")

    def _planned(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Expand the daily plan into events between start and end."""
        status = self.coordinator.data
        schedule = status.schedule
        if not schedule.auto_feed:
            return []
        tz_offset = status.tz_offset
        device_today = device_now(tz_offset).date()
        now = dt_util.utcnow()
        events: list[CalendarEvent] = []
        day = (start + timedelta(seconds=tz_offset)).date() - timedelta(days=1)
        last = (end + timedelta(seconds=tz_offset)).date()
        while day <= last:
            for entry in schedule.entries:
                begin = self._device_time(day, entry, tz_offset)
                if begin < now or not start <= begin < end:
                    continue
                if day == device_today and not entry.enabled:
                    continue  # skipped for today only, like in the app
                events.append(self._plan_event(entry, begin))
            day += timedelta(days=1)
        return events

    @staticmethod
    def _device_time(day: date, entry: ScheduleEntry, tz_offset: int) -> datetime:
        naive = datetime(day.year, day.month, day.day, entry.hour, entry.minute)
        return (naive - timedelta(seconds=tz_offset)).replace(tzinfo=dt_util.UTC)

    def _plan_event(self, entry: ScheduleEntry, begin: datetime) -> CalendarEvent:
        grams = entry.portions * GRAMS_PER_PORTION
        return CalendarEvent(
            start=begin,
            end=begin + EVENT_LENGTH,
            summary=f"🍽 {entry.portions} x ({grams} g)",
            description=f"Planned feeding: {entry.portions} portions (~{grams} g)",
            uid=f"plan-{self.feeder.did}-{int(begin.timestamp())}",
        )

    @staticmethod
    def _record_event(record: FeedRecord, did: str) -> CalendarEvent:
        begin = record.time
        if record.kind == "meal":
            summary = f"😺 {record.grams} g"
            description = f"Pet ate {record.grams} g"
        else:
            ok = record.result == FeedResult.OK
            summary = f"{'✅' if ok else '⚠️'} {record.portions} x ({record.source})"
            description = (
                f"Dispensed {record.portions} portions, source: {record.source}, "
                f"result: {record.result}"
            )
        return CalendarEvent(
            start=begin,
            end=begin + EVENT_LENGTH,
            summary=summary,
            description=description,
            uid=f"{record.kind}-{did}-{record.ts}",
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Next planned feeding."""
        now = dt_util.utcnow()
        upcoming = self._planned(now, now + timedelta(days=2))
        return min(upcoming, key=lambda e: e.start) if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Planned feedings ahead plus recorded history behind."""
        events = self._planned(start_date, end_date)
        now = dt_util.utcnow()
        oldest = now - timedelta(days=HISTORY_DAYS)
        history_start = max(start_date, oldest)
        history_end = min(end_date, now)
        if history_start < history_end:
            status = self.coordinator.data
            try:
                records = await self.feeder.api.records_between(
                    status.tz_offset,
                    int(history_start.timestamp()),
                    int(history_end.timestamp()),
                    status.dst,
                )
            except XiaomiError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="update_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            events.extend(self._record_event(r, self.feeder.did) for r in records)
        events.sort(key=lambda e: e.start)
        return events
