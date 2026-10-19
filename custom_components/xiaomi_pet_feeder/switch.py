"""Switches: the toggles of the app (home card, schedule page and settings)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Action, FeederStatus, Prop
from .commands import current_schedule, device_minutes, write_schedule
from .data import Feeder, XiaomiPetFeederConfigEntry
from .entity import FeederStatusEntity, run_command

PARALLEL_UPDATES = 1


async def _set_auto_feed(feeder: Feeder, on: bool) -> None:
    schedule = current_schedule(feeder).with_auto_feed(on, device_minutes(feeder))
    await write_schedule(feeder, schedule)


async def _set_low_intake_notify(feeder: Feeder, on: bool) -> None:
    status = feeder.status.data
    threshold = int(status.get(Prop.LOW_INTAKE_THRESHOLD) or 10)
    await feeder.api.action(Action.LOW_INTAKE_SETTINGS, threshold, on)
    feeder.status.apply(Prop.LOW_INTAKE_NOTIFY, on)


async def _set_refill_reminder(feeder: Feeder, on: bool) -> None:
    hours = int(feeder.status.data.get(Prop.EMPTY_BOWL_HOURS) or 8)
    await feeder.api.action(Action.REFILL_SETTINGS, on, hours)
    feeder.status.apply(Prop.REFILL_REMINDER, on)


def _setter(prop: tuple[int, int], as_int: bool) -> Callable[[Feeder, bool], Awaitable[None]]:
    async def _set(feeder: Feeder, on: bool) -> None:
        value: Any = int(on) if as_int else on
        await feeder.api.set_property(prop, value)
        feeder.status.apply(prop, value)

    return _set


@dataclass(frozen=True, kw_only=True)
class FeederSwitchDescription(SwitchEntityDescription):
    """Switch with its read and write logic."""

    is_on_fn: Callable[[FeederStatus], bool | None]
    set_fn: Callable[[Feeder, bool], Awaitable[None]]
    writes_schedule: bool = False


SWITCHES: tuple[FeederSwitchDescription, ...] = (
    FeederSwitchDescription(
        key="auto_feed",
        translation_key="auto_feed",
        is_on_fn=lambda s: s.schedule.auto_feed,
        set_fn=_set_auto_feed,
        writes_schedule=True,
    ),
    FeederSwitchDescription(
        key="low_intake_notification",
        translation_key="low_intake_notification",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda s: s.flag(Prop.LOW_INTAKE_NOTIFY),
        set_fn=_set_low_intake_notify,
    ),
    FeederSwitchDescription(
        key="refill_reminder",
        translation_key="refill_reminder",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda s: s.flag(Prop.REFILL_REMINDER),
        set_fn=_set_refill_reminder,
    ),
    FeederSwitchDescription(
        key="dispense_compensation",
        translation_key="dispense_compensation",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda s: s.flag(Prop.COMPENSATION),
        set_fn=_setter(Prop.COMPENSATION, as_int=True),
    ),
    FeederSwitchDescription(
        key="prevent_spillage",
        translation_key="prevent_spillage",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda s: s.flag(Prop.PREVENT_HEAP),
        set_fn=_setter(Prop.PREVENT_HEAP, as_int=True),
    ),
    FeederSwitchDescription(
        key="auto_screen_off",
        translation_key="auto_screen_off",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda s: s.flag(Prop.AUTO_SCREEN_OFF),
        set_fn=_setter(Prop.AUTO_SCREEN_OFF, as_int=True),
    ),
    FeederSwitchDescription(
        key="child_lock",
        translation_key="child_lock",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda s: s.flag(Prop.CHILD_LOCK),
        set_fn=_setter(Prop.CHILD_LOCK, as_int=False),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaomiPetFeederConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    async_add_entities(
        FeederSwitch(feeder, description)
        for feeder in entry.runtime_data.feeders.values()
        for description in SWITCHES
    )


class FeederSwitch(FeederStatusEntity, SwitchEntity):
    """A toggle of the app."""

    entity_description: FeederSwitchDescription

    def __init__(self, feeder: Feeder, description: FeederSwitchDescription) -> None:
        """Initialize."""
        super().__init__(feeder, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Current state."""
        return self.entity_description.is_on_fn(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on."""
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        await self._write(False)

    async def _write(self, on: bool) -> None:
        if self.entity_description.writes_schedule:
            # write_schedule already validates and translates errors.
            await self.entity_description.set_fn(self.feeder, on)
            return
        await run_command(self.feeder, self.entity_description.set_fn(self.feeder, on))
        await self.coordinator.async_request_refresh()
