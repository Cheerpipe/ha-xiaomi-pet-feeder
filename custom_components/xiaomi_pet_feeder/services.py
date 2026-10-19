"""Integration services: dispense, edit the feeding plan and read history."""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
import voluptuous as vol

from .api import FeedingSchedule, ScheduleEntry, XiaomiError, device_midnight, device_now
from .api.schedule import MAX_PORTIONS, MIN_PORTIONS
from .commands import current_schedule, device_minutes, dispense, write_schedule
from .const import (
    ATTR_AUTO_FEED,
    ATTR_DATE,
    ATTR_DAYS,
    ATTR_ENABLED,
    ATTR_ENTRIES,
    ATTR_PORTIONS,
    ATTR_TIME,
    DOMAIN,
)
from .data import Feeder

SERVICE_DISPENSE = "dispense"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_ADD_ENTRY = "add_schedule_entry"
SERVICE_REMOVE_ENTRY = "remove_schedule_entry"
SERVICE_SKIP_TODAY = "set_schedule_entry_today"
SERVICE_GET_SCHEDULE = "get_schedule"
SERVICE_GET_HISTORY = "get_feeding_history"
SERVICE_GET_HOURLY = "get_hourly_stats"
SERVICE_GET_DAILY = "get_daily_stats"

PORTIONS = vol.All(vol.Coerce(int), vol.Range(min=MIN_PORTIONS, max=MAX_PORTIONS))
DEVICE = {vol.Required(ATTR_DEVICE_ID): cv.string}

ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TIME): cv.time,
        vol.Required(ATTR_PORTIONS): PORTIONS,
        vol.Optional(ATTR_ENABLED, default=True): cv.boolean,
    }
)

SCHEMAS: dict[str, vol.Schema] = {
    SERVICE_DISPENSE: vol.Schema({**DEVICE, vol.Required(ATTR_PORTIONS): PORTIONS}),
    SERVICE_SET_SCHEDULE: vol.Schema(
        {
            **DEVICE,
            vol.Required(ATTR_ENTRIES): vol.All(cv.ensure_list, [ENTRY_SCHEMA]),
            vol.Optional(ATTR_AUTO_FEED): cv.boolean,
        }
    ),
    SERVICE_ADD_ENTRY: vol.Schema(
        {**DEVICE, vol.Required(ATTR_TIME): cv.time, vol.Required(ATTR_PORTIONS): PORTIONS}
    ),
    SERVICE_REMOVE_ENTRY: vol.Schema({**DEVICE, vol.Required(ATTR_TIME): cv.time}),
    SERVICE_SKIP_TODAY: vol.Schema(
        {**DEVICE, vol.Required(ATTR_TIME): cv.time, vol.Required(ATTR_ENABLED): cv.boolean}
    ),
    SERVICE_GET_SCHEDULE: vol.Schema(DEVICE),
    SERVICE_GET_HISTORY: vol.Schema(
        {**DEVICE, vol.Optional(ATTR_DAYS, default=1): vol.All(int, vol.Range(min=1, max=90))}
    ),
    SERVICE_GET_HOURLY: vol.Schema({**DEVICE, vol.Optional(ATTR_DATE): cv.date}),
    SERVICE_GET_DAILY: vol.Schema({**DEVICE, vol.Optional(ATTR_DATE): cv.date}),
}


def _feeder(hass: HomeAssistant, call: ServiceCall) -> Feeder:
    """Resolve the feeder targeted by a service call."""
    device_id = call.data[ATTR_DEVICE_ID]
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_device",
            translation_placeholders={"device_id": device_id},
        )
    did = next((i for d, i in device.identifiers if d == DOMAIN), None)
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and did in entry.runtime_data.feeders:
            return entry.runtime_data.feeders[did]
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_device",
        translation_placeholders={"device_id": device_id},
    )


def _entry(value: time, portions: int, enabled: bool = True) -> ScheduleEntry:
    return ScheduleEntry(value.hour, value.minute, portions, enabled)


def _schedule_response(feeder: Feeder) -> dict[str, Any]:
    schedule = current_schedule(feeder)
    return {
        "auto_feed": schedule.auto_feed,
        "entries": [
            {"time": e.time_str, "portions": e.portions, "enabled_today": e.enabled}
            for e in schedule.entries
        ],
    }


async def _cloud(coro: Any) -> Any:
    try:
        return await coro
    except XiaomiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": str(err)},
        ) from err


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services once, from async_setup."""

    async def handle_dispense(call: ServiceCall) -> None:
        await dispense(_feeder(hass, call), call.data[ATTR_PORTIONS])

    async def handle_set_schedule(call: ServiceCall) -> None:
        feeder = _feeder(hass, call)
        schedule = current_schedule(feeder)
        if ATTR_AUTO_FEED in call.data:
            schedule = schedule.with_auto_feed(call.data[ATTR_AUTO_FEED], device_minutes(feeder))
        entries = [_entry(e[ATTR_TIME], e[ATTR_PORTIONS]) for e in call.data[ATTR_ENTRIES]]
        schedule = schedule.with_entries(entries, device_minutes(feeder))
        await write_schedule(feeder, schedule)

    async def handle_add(call: ServiceCall) -> None:
        feeder = _feeder(hass, call)
        schedule = current_schedule(feeder)
        new = _entry(call.data[ATTR_TIME], call.data[ATTR_PORTIONS])
        others = [e for e in schedule.entries if (e.hour, e.minute) != (new.hour, new.minute)]
        now = device_minutes(feeder)
        added = ScheduleEntry(new.hour, new.minute, new.portions, new.minutes > now)
        await write_schedule(feeder, FeedingSchedule(schedule.auto_feed, (*others, added)))

    async def handle_remove(call: ServiceCall) -> None:
        feeder = _feeder(hass, call)
        schedule = current_schedule(feeder)
        value: time = call.data[ATTR_TIME]
        if schedule.find(value.hour, value.minute) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
                translation_placeholders={"time": value.strftime("%H:%M")},
            )
        remaining = tuple(
            e for e in schedule.entries if (e.hour, e.minute) != (value.hour, value.minute)
        )
        await write_schedule(feeder, FeedingSchedule(schedule.auto_feed, remaining))

    async def handle_skip_today(call: ServiceCall) -> None:
        feeder = _feeder(hass, call)
        schedule = current_schedule(feeder)
        value: time = call.data[ATTR_TIME]
        entry = schedule.find(value.hour, value.minute)
        if entry is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
                translation_placeholders={"time": value.strftime("%H:%M")},
            )
        updated = tuple(
            ScheduleEntry(e.hour, e.minute, e.portions, call.data[ATTR_ENABLED])
            if e is entry
            else e
            for e in schedule.entries
        )
        await write_schedule(feeder, FeedingSchedule(schedule.auto_feed, updated))

    async def handle_get_schedule(call: ServiceCall) -> ServiceResponse:
        return _schedule_response(_feeder(hass, call))

    async def handle_history(call: ServiceCall) -> ServiceResponse:
        feeder = _feeder(hass, call)
        status = feeder.status.data
        end = device_now(status.tz_offset)
        start_day = end.date() - timedelta(days=call.data[ATTR_DAYS] - 1)
        records = await _cloud(
            feeder.api.records_between(
                status.tz_offset,
                device_midnight(status.tz_offset, start_day),
                device_midnight(status.tz_offset, end.date()) + 86399,
                status.dst,
            )
        )
        return {"records": [r.as_dict() for r in records]}

    async def handle_hourly(call: ServiceCall) -> ServiceResponse:
        feeder = _feeder(hass, call)
        status = feeder.status.data
        day: date = call.data.get(ATTR_DATE) or device_now(status.tz_offset).date()
        return await _cloud(feeder.api.hourly(status.tz_offset, day))

    async def handle_daily(call: ServiceCall) -> ServiceResponse:
        feeder = _feeder(hass, call)
        status = feeder.status.data
        day: date = call.data.get(ATTR_DATE) or device_now(status.tz_offset).date()
        # Same week as the app: Sunday to Saturday.
        start = day - timedelta(days=(day.weekday() + 1) % 7)
        return await _cloud(feeder.api.daily(status.tz_offset, start, start + timedelta(days=6)))

    handlers = {
        SERVICE_DISPENSE: (handle_dispense, SupportsResponse.NONE),
        SERVICE_SET_SCHEDULE: (handle_set_schedule, SupportsResponse.NONE),
        SERVICE_ADD_ENTRY: (handle_add, SupportsResponse.NONE),
        SERVICE_REMOVE_ENTRY: (handle_remove, SupportsResponse.NONE),
        SERVICE_SKIP_TODAY: (handle_skip_today, SupportsResponse.NONE),
        SERVICE_GET_SCHEDULE: (handle_get_schedule, SupportsResponse.ONLY),
        SERVICE_GET_HISTORY: (handle_history, SupportsResponse.ONLY),
        SERVICE_GET_HOURLY: (handle_hourly, SupportsResponse.ONLY),
        SERVICE_GET_DAILY: (handle_daily, SupportsResponse.ONLY),
    }
    for name, (handler, response) in handlers.items():
        hass.services.async_register(
            DOMAIN, name, handler, schema=SCHEMAS[name], supports_response=response
        )
