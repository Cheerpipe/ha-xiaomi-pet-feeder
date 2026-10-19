"""Event entities: a dispense finished, or the pet ate."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import FeedResult
from .data import Feeder, XiaomiPetFeederConfigEntry
from .entity import FeederRecordsEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaomiPetFeederConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up event entities."""
    entities: list[EventEntity] = []
    for feeder in entry.runtime_data.feeders.values():
        entities += [DispenseEvent(feeder), MealEvent(feeder)]
    async_add_entities(entities)


class DispenseEvent(FeederRecordsEntity, EventEntity):
    """Fired for every dispense recorded in the cloud (app, button or plan)."""

    _attr_translation_key = "dispense"
    _attr_event_types = ["dispensed", "failed"]

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "dispense_event")

    @callback
    def _handle_coordinator_update(self) -> None:
        for record in self.coordinator.data.new:
            if record.kind != "dispense":
                continue
            self._trigger_event(
                "dispensed" if record.result == FeedResult.OK else "failed",
                {
                    "portions": record.portions,
                    "source": record.source,
                    "result": record.result,
                    "time": record.time.isoformat(),
                },
            )
        super()._handle_coordinator_update()


class MealEvent(FeederRecordsEntity, EventEntity):
    """Fired when the cloud records that the pet ate."""

    _attr_translation_key = "meal"
    _attr_event_types = ["ate"]

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "meal_event")

    @callback
    def _handle_coordinator_update(self) -> None:
        for record in self.coordinator.data.new:
            if record.kind == "meal":
                self._trigger_event("ate", {"grams": record.grams, "time": record.time.isoformat()})
        super()._handle_coordinator_update()
