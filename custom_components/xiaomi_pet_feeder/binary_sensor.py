"""Binary sensors: the alerts shown on the app's home screen."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Prop
from .data import Feeder, XiaomiPetFeederConfigEntry
from .entity import FeederStatusEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class FeederBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor backed by one MIoT property."""

    prop: tuple[int, int]


BINARY_SENSORS: tuple[FeederBinarySensorDescription, ...] = (
    FeederBinarySensorDescription(
        key="food_stuck",
        translation_key="food_stuck",
        device_class=BinarySensorDeviceClass.PROBLEM,
        prop=Prop.FOOD_STUCK,
    ),
    FeederBinarySensorDescription(
        key="food_low",
        translation_key="food_low",
        device_class=BinarySensorDeviceClass.PROBLEM,
        prop=Prop.FOOD_LOW,
    ),
    FeederBinarySensorDescription(
        key="bowl_error",
        translation_key="bowl_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        prop=Prop.BOWL_ABNORMAL,
    ),
    FeederBinarySensorDescription(
        key="low_intake",
        translation_key="low_intake",
        device_class=BinarySensorDeviceClass.PROBLEM,
        prop=Prop.LOW_INTAKE,
    ),
    FeederBinarySensorDescription(
        key="refill_due",
        translation_key="refill_due",
        device_class=BinarySensorDeviceClass.PROBLEM,
        prop=Prop.REFILL_DUE,
    ),
    FeederBinarySensorDescription(
        key="weight_sensor_error",
        translation_key="weight_sensor_error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        prop=Prop.SENSOR_FAULT,
    ),
    FeederBinarySensorDescription(
        key="bowl_full",
        translation_key="bowl_full",
        device_class=BinarySensorDeviceClass.PROBLEM,
        prop=Prop.BOWL_HEAP,
    ),
    FeederBinarySensorDescription(
        key="dispensing",
        translation_key="dispensing",
        device_class=BinarySensorDeviceClass.RUNNING,
        prop=Prop.DISPENSING,
    ),
    FeederBinarySensorDescription(
        key="battery_low",
        translation_key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        prop=Prop.BATTERY_LOW,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaomiPetFeederConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    async_add_entities(
        FeederBinarySensor(feeder, description)
        for feeder in entry.runtime_data.feeders.values()
        for description in BINARY_SENSORS
    )


class FeederBinarySensor(FeederStatusEntity, BinarySensorEntity):
    """One alert flag."""

    entity_description: FeederBinarySensorDescription

    def __init__(self, feeder: Feeder, description: FeederBinarySensorDescription) -> None:
        """Initialize."""
        super().__init__(feeder, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Whether the flag is raised."""
        return self.coordinator.data.flag(self.entity_description.prop)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Hours shown in the app's "Time to refill" banner."""
        if self.entity_description.prop != Prop.REFILL_DUE:
            return None
        data = self.coordinator.data
        hours = data.get(Prop.REFILL_TRIGGER_HOURS) or data.get(Prop.EMPTY_BOWL_HOURS) or 8
        return {"empty_bowl_hours": hours}
