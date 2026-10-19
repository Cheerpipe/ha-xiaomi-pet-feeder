"""Sensors of the Xiaomi Smart Pet Feeder."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import DesiccantState, FeederStatus, Prop
from .coordinator import FeederRecords
from .data import Feeder, XiaomiPetFeederConfigEntry
from .entity import FeederRecordsEntity, FeederStatusEntity, device_info

PARALLEL_UPDATES = 0

STATUS_OPTIONS = [
    "dispensing",
    "food_stuck",
    "food_low",
    "bowl_error",
    "low_intake",
    "refill_due",
    "schedule_running",
    "schedule_completed",
    "no_schedule",
]


def feeder_state(status: FeederStatus) -> str:
    """Return the state with the same priority as the app's home screen."""
    for prop, state in (
        (Prop.DISPENSING, "dispensing"),
        (Prop.FOOD_STUCK, "food_stuck"),
        (Prop.FOOD_LOW, "food_low"),
        (Prop.BOWL_ABNORMAL, "bowl_error"),
        (Prop.LOW_INTAKE, "low_intake"),
        (Prop.REFILL_DUE, "refill_due"),
    ):
        if status.flag(prop):
            return state
    schedule = status.schedule
    if not schedule.auto_feed or not schedule.entries:
        return "no_schedule"
    progress = status.get(Prop.SCHEDULE_PROGRESS) or 0
    return "schedule_completed" if progress >= 100 else "schedule_running"


@dataclass(frozen=True, kw_only=True)
class StatusSensorDescription(SensorEntityDescription):
    """Sensor fed by MIoT properties."""

    value_fn: Callable[[FeederStatus], Any]
    available_fn: Callable[[FeederStatus], bool] = lambda _: True


STATUS_SENSORS: tuple[StatusSensorDescription, ...] = (
    StatusSensorDescription(
        key="bowl_food",
        translation_key="bowl_food",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        value_fn=lambda s: s.get(Prop.BOWL_WEIGHT),
    ),
    StatusSensorDescription(
        key="schedule_progress",
        translation_key="schedule_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.get(Prop.SCHEDULE_PROGRESS),
        available_fn=lambda s: s.schedule.auto_feed and bool(s.schedule.entries),
    ),
    StatusSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUS_OPTIONS,
        value_fn=feeder_state,
    ),
    StatusSensorDescription(
        key="dispense_progress",
        translation_key="dispense_progress",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.get(Prop.DISPENSE_PROGRESS),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaomiPetFeederConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    entities: list[SensorEntity] = []
    for feeder in entry.runtime_data.feeders.values():
        entities.extend(StatusSensor(feeder, d) for d in STATUS_SENSORS)
        entities.extend(
            [
                EatenTodaySensor(feeder),
                FeedingLogSensor(feeder),
                DesiccantSensor(feeder),
                UpdateModeSensor(feeder),
                LastPushSensor(feeder),
            ]
        )
    async_add_entities(entities)


class StatusSensor(FeederStatusEntity, SensorEntity):
    """Sensor fed by MIoT properties."""

    entity_description: StatusSensorDescription

    def __init__(self, feeder: Feeder, description: StatusSensorDescription) -> None:
        """Initialize."""
        super().__init__(feeder, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Follow the app: some values only make sense in some states."""
        return (
            super().available
            and self.coordinator.data is not None
            and self.entity_description.available_fn(self.coordinator.data)
        )

    @property
    def native_value(self) -> Any:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data)


class EatenTodaySensor(FeederRecordsEntity, SensorEntity):
    """The app's "Food eaten today", computed by Xiaomi's cloud."""

    _attr_translation_key = "eaten_today"
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_suggested_display_precision = 0

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "eaten_today")

    @property
    def native_value(self) -> int:
        """Grams eaten since device midnight."""
        return self.coordinator.data.eaten_today


class FeedingLogSensor(FeederRecordsEntity, SensorEntity):
    """Today's feeding log: number of entries, details in attributes."""

    _attr_translation_key = "feeding_log"
    _unrecorded_attributes = frozenset({"records"})

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "feeding_log")

    @property
    def native_value(self) -> int:
        """Number of meals and dispenses today."""
        return len(self.coordinator.data.today)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Today's records, newest first, like the app."""
        data: FeederRecords = self.coordinator.data
        return {"records": [r.as_dict() for r in reversed(data.today)]}


class DesiccantSensor(FeederRecordsEntity, SensorEntity):
    """Desiccant cartridge status from the consumables API."""

    _attr_translation_key = "desiccant"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.value for s in DesiccantState]

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "desiccant")

    @property
    def native_value(self) -> str | None:
        """Sufficient, insufficient, exhausted or unknown."""
        desiccant = self.coordinator.data.desiccant
        return desiccant.state.value if desiccant else DesiccantState.UNKNOWN.value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Level and remaining life as reported by the cloud."""
        desiccant = self.coordinator.data.desiccant
        if desiccant is None:
            return None
        return {
            "level": desiccant.level,
            "days_left": None
            if desiccant.hours_left is None
            else round(desiccant.hours_left / 24, 1),
            "lifetime_days": None
            if desiccant.total_hours is None
            else round(desiccant.total_hours / 24, 1),
        }


class FeederPushSensor(SensorEntity):
    """Diagnostic sensor that follows the real-time updates from Xiaomi Home."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, feeder: Feeder, key: str) -> None:
        """Initialize."""
        self.feeder = feeder
        self._attr_unique_id = f"{feeder.did}_{key}"
        self._attr_device_info = device_info(feeder)

    async def async_added_to_hass(self) -> None:
        """Follow the listener of the official event entities."""
        await super().async_added_to_hass()
        if self.feeder.push is not None:
            self.async_on_remove(self.feeder.push.async_add_listener(self.async_write_ha_state))


class UpdateModeSensor(FeederPushSensor):
    """Push when the official integration's events are available, polling otherwise."""

    _attr_translation_key = "update_mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["push", "polling"]

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "update_mode")

    @property
    def native_value(self) -> str:
        """Current mode."""
        push = self.feeder.push
        return "push" if push is not None and push.active else "polling"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Which official event entities are followed."""
        push = self.feeder.push
        return {
            "push_enabled": push is not None,
            "official_entities": list(push.entity_ids) if push else [],
        }


class LastPushSensor(FeederPushSensor, RestoreSensor):
    """When the last event pushed by the official integration was received."""

    _attr_translation_key = "last_push"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "last_push")
        self._restored: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Keep the last value across restarts."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None and isinstance(
            last.native_value, datetime
        ):
            self._restored = last.native_value

    @property
    def native_value(self) -> datetime | None:
        """Time of the last pushed event."""
        push = self.feeder.push
        return (push.last_push if push else None) or self._restored
