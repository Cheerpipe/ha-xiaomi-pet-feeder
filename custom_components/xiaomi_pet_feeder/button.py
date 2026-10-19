"""Buttons: dispense, calibration, dismissing alerts and desiccant reset."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Action, Prop
from .commands import dispense
from .const import DOMAIN
from .data import Feeder, XiaomiPetFeederConfigEntry
from .entity import FeederStatusEntity, run_command

PARALLEL_UPDATES = 1


def _portions(button: FeederButton) -> int:
    """Value of the Portions number entity of the same feeder."""
    registry = er.async_get(button.hass)
    entity_id = registry.async_get_entity_id("number", DOMAIN, f"{button.feeder.did}_portions")
    if entity_id and (state := button.hass.states.get(entity_id)):
        try:
            return int(float(state.state))
        except ValueError:
            pass
    return 1


async def _dispense(button: FeederButton) -> None:
    await dispense(button.feeder, _portions(button))


async def _calibrate(button: FeederButton) -> None:
    await run_command(button.feeder, button.feeder.api.action(Action.CALIBRATE))


async def _clear_low_intake(button: FeederButton) -> None:
    await run_command(button.feeder, button.feeder.api.action(Action.CLEAR_LOW_INTAKE, 0))
    button.feeder.status.apply(Prop.LOW_INTAKE, 0)


async def _clear_refill(button: FeederButton) -> None:
    await run_command(button.feeder, button.feeder.api.action(Action.CLEAR_REFILL, 0))
    button.feeder.status.apply(Prop.REFILL_DUE, 0)


async def _reset_desiccant(button: FeederButton) -> None:
    await run_command(button.feeder, button.feeder.api.action(Action.RESET_DESICCANT))
    button.feeder.records.invalidate_desiccant()
    await button.feeder.records.async_request_refresh()


@dataclass(frozen=True, kw_only=True)
class FeederButtonDescription(ButtonEntityDescription):
    """Button with its press handler."""

    press_fn: Callable[[FeederButton], Awaitable[None]]


BUTTONS: tuple[FeederButtonDescription, ...] = (
    FeederButtonDescription(key="dispense", translation_key="dispense", press_fn=_dispense),
    FeederButtonDescription(
        key="calibrate",
        translation_key="calibrate",
        entity_category=EntityCategory.CONFIG,
        press_fn=_calibrate,
    ),
    FeederButtonDescription(
        key="dismiss_low_intake",
        translation_key="dismiss_low_intake",
        press_fn=_clear_low_intake,
    ),
    FeederButtonDescription(
        key="dismiss_refill",
        translation_key="dismiss_refill",
        press_fn=_clear_refill,
    ),
    FeederButtonDescription(
        key="reset_desiccant",
        translation_key="reset_desiccant",
        entity_category=EntityCategory.CONFIG,
        press_fn=_reset_desiccant,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XiaomiPetFeederConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    async_add_entities(
        FeederButton(feeder, description)
        for feeder in entry.runtime_data.feeders.values()
        for description in BUTTONS
    )


class FeederButton(FeederStatusEntity, ButtonEntity):
    """A one-shot command."""

    entity_description: FeederButtonDescription

    def __init__(self, feeder: Feeder, description: FeederButtonDescription) -> None:
        """Initialize."""
        super().__init__(feeder, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Run the command."""
        await self.entity_description.press_fn(self)
        await self.coordinator.async_request_refresh()
