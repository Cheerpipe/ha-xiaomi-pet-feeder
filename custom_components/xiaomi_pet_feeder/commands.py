"""Commands shared by entities and services, with the same guards as the app."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.exceptions import ServiceValidationError

from .api import Action, FeedingSchedule, Prop, ScheduleError, device_now
from .api.schedule import MAX_PORTIONS, MIN_PORTIONS
from .const import DOMAIN
from .data import Feeder
from .entity import run_command

# The cloud receives the dispense/meal events a little after they happen.
_RECORD_REFRESH_DELAYS = (timedelta(seconds=20), timedelta(seconds=90))


def device_minutes(feeder: Feeder) -> int:
    """Minutes since midnight in device time."""
    now = device_now(feeder.records.tz_offset)
    return now.hour * 60 + now.minute


def refresh_records_soon(feeder: Feeder) -> None:
    """Pick up new cloud records shortly after a command."""
    feeder.records.refresh_soon(*_RECORD_REFRESH_DELAYS)


async def dispense(feeder: Feeder, portions: int) -> None:
    """Dispense food now, refusing when the app would refuse too."""
    if not MIN_PORTIONS <= portions <= MAX_PORTIONS:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_portions",
            translation_placeholders={"min": str(MIN_PORTIONS), "max": str(MAX_PORTIONS)},
        )
    status = feeder.status.data
    if status is not None:
        if status.flag(Prop.DISPENSING):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="busy")
        if status.flag(Prop.FOOD_STUCK):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="food_stuck")
    await run_command(feeder, feeder.api.action(Action.DISPENSE, portions))
    feeder.status.apply(Prop.DISPENSING, 1)
    await feeder.status.async_request_refresh()
    refresh_records_soon(feeder)


async def write_schedule(feeder: Feeder, schedule: FeedingSchedule) -> None:
    """Validate and store a feeding plan (action 5.2, like the app)."""
    try:
        schedule.validate()
    except ScheduleError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.key,
            translation_placeholders=err.placeholders,
        ) from err
    encoded = schedule.encode()
    await run_command(feeder, feeder.api.action(Action.WRITE_SCHEDULE, encoded))
    feeder.status.apply(Prop.SCHEDULE, encoded)
    await feeder.status.async_request_refresh()


def current_schedule(feeder: Feeder) -> FeedingSchedule:
    """Plan as last read from the device."""
    if feeder.status.data is None:
        return FeedingSchedule(auto_feed=False)
    return feeder.status.data.schedule
