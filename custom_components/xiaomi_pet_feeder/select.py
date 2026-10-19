"""Select: what the feeder shows on its screen (setting added in plugin 1.1.x)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Prop
from .data import Feeder, XiaomiPetFeederConfigEntry
from .entity import FeederStatusEntity, run_command

PARALLEL_UPDATES = 1

SCREEN_OPTIONS = ["bowl_food", "eaten_today", "schedule_progress"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaomiPetFeederConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the screen display select on firmwares that expose it."""
    async_add_entities(
        ScreenDisplaySelect(feeder)
        for feeder in entry.runtime_data.feeders.values()
        if feeder.status.data and feeder.status.data.get(Prop.SCREEN_DISPLAY) is not None
    )


class ScreenDisplaySelect(FeederStatusEntity, SelectEntity):
    """Data type displayed on the device's screen (MIoT 5.18)."""

    _attr_translation_key = "screen_display"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = SCREEN_OPTIONS

    def __init__(self, feeder: Feeder) -> None:
        """Initialize."""
        super().__init__(feeder, "screen_display")

    @property
    def current_option(self) -> str | None:
        """Current option."""
        value = self.coordinator.data.get(Prop.SCREEN_DISPLAY)
        if isinstance(value, int) and 0 <= value < len(SCREEN_OPTIONS):
            return SCREEN_OPTIONS[value]
        return None

    async def async_select_option(self, option: str) -> None:
        """Write the option (plain property write, like the app)."""
        value = SCREEN_OPTIONS.index(option)
        await run_command(self.feeder, self.feeder.api.set_property(Prop.SCREEN_DISPLAY, value))
        self.coordinator.apply(Prop.SCREEN_DISPLAY, value)
        await self.coordinator.async_request_refresh()
