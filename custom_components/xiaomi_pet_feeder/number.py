"""Numbers: portions to dispense and the numeric settings of the app."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Action, FeederStatus, Prop
from .api.schedule import MAX_PORTIONS, MIN_PORTIONS
from .data import Feeder, XiaomiPetFeederConfigEntry
from .entity import FeederStatusEntity, device_info, run_command

PARALLEL_UPDATES = 1


async def _set_threshold(feeder: Feeder, value: int) -> None:
    notify = bool(feeder.status.data.flag(Prop.LOW_INTAKE_NOTIFY))
    await feeder.api.action(Action.LOW_INTAKE_SETTINGS, value, notify)
    feeder.status.apply(Prop.LOW_INTAKE_THRESHOLD, value)


async def _set_empty_bowl_hours(feeder: Feeder, value: int) -> None:
    reminder = bool(feeder.status.data.flag(Prop.REFILL_REMINDER))
    await feeder.api.action(Action.REFILL_SETTINGS, reminder, value)
    feeder.status.apply(Prop.EMPTY_BOWL_HOURS, value)


@dataclass(frozen=True, kw_only=True)
class FeederNumberDescription(NumberEntityDescription):
    """Number backed by a property and written with an action."""

    value_fn: Callable[[FeederStatus], int | None]
    set_fn: Callable[[Feeder, int], Awaitable[None]]
    available_fn: Callable[[FeederStatus], bool] = lambda _: True


NUMBERS: tuple[FeederNumberDescription, ...] = (
    FeederNumberDescription(
        key="low_intake_threshold",
        translation_key="low_intake_threshold",
        entity_category=EntityCategory.CONFIG,
        native_min_value=10,
        native_max_value=90,
        native_step=10,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        value_fn=lambda s: s.get(Prop.LOW_INTAKE_THRESHOLD),
        set_fn=_set_threshold,
    ),
    FeederNumberDescription(
        key="empty_bowl_duration",
        translation_key="empty_bowl_duration",
        entity_category=EntityCategory.CONFIG,
        native_min_value=6,
        native_max_value=12,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.HOURS,
        mode=NumberMode.BOX,
        value_fn=lambda s: s.get(Prop.EMPTY_BOWL_HOURS),
        set_fn=_set_empty_bowl_hours,
        available_fn=lambda s: bool(s.flag(Prop.REFILL_REMINDER)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaomiPetFeederConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up numbers."""
    entities: list[NumberEntity] = []
    for feeder in entry.runtime_data.feeders.values():
        entities.append(PortionsNumber(feeder))
        entities.extend(FeederNumber(feeder, d) for d in NUMBERS)
    async_add_entities(entities)


class FeederNumber(FeederStatusEntity, NumberEntity):
    """A numeric setting of the app."""

    entity_description: FeederNumberDescription

    def __init__(self, feeder: Feeder, description: FeederNumberDescription) -> None:
        """Initialize."""
        super().__init__(feeder, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """The app greys out the bowl duration when the reminder is off."""
        return super().available and self.entity_description.available_fn(self.coordinator.data)

    @property
    def native_value(self) -> float | None:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_set_native_value(self, value: float) -> None:
        """Write the new value."""
        await run_command(self.feeder, self.entity_description.set_fn(self.feeder, int(value)))
        await self.coordinator.async_request_refresh()


class PortionsNumber(RestoreNumber):
    """How many portions the Dispense button gives (the app's portion picker)."""

    _attr_has_entity_name = True
    _attr_translation_key = "portions"
    _attr_native_min_value = MIN_PORTIONS
    _attr_native_max_value = MAX_PORTIONS
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        self.feeder = feeder
        self._attr_unique_id = f"{feeder.did}_portions"
        self._attr_device_info = device_info(feeder)
        self._attr_native_value = 1

    async def async_added_to_hass(self) -> None:
        """Restore the last chosen amount."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value:
            self._attr_native_value = last.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Store the amount locally."""
        self._attr_native_value = int(value)
        self.async_write_ha_state()
