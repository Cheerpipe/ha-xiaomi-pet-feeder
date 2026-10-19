"""Base entities for the Xiaomi Smart Pet Feeder."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Prop, XiaomiAuthError, XiaomiError
from .const import DOMAIN, MANUFACTURER
from .coordinator import FeederRecordsCoordinator, FeederStatusCoordinator
from .data import Feeder


def device_info(feeder: Feeder) -> DeviceInfo:
    """Device registry entry of a feeder."""
    firmware = None
    if feeder.status.data:
        firmware = feeder.status.data.get(Prop.FIRMWARE)
    return DeviceInfo(
        identifiers={(DOMAIN, feeder.did)},
        manufacturer=MANUFACTURER,
        model="Smart Pet Food Feeder 2",
        model_id=feeder.model,
        name=feeder.name,
        sw_version=str(firmware) if firmware else None,
    )


async def run_command(feeder: Feeder, command: Awaitable[Any]) -> None:
    """Run a cloud command and translate errors for the UI."""
    try:
        await command
    except XiaomiAuthError as err:
        feeder.status.config_entry.async_start_reauth(feeder.status.hass)
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_expired") from err
    except XiaomiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": str(err)},
        ) from err


class FeederStatusEntity(CoordinatorEntity[FeederStatusCoordinator]):
    """Entity backed by the MIoT properties."""

    _attr_has_entity_name = True

    def __init__(self, feeder: Feeder, key: str) -> None:
        """Initialize."""
        super().__init__(feeder.status)
        self.feeder = feeder
        self._attr_unique_id = f"{feeder.did}_{key}"
        self._attr_device_info = device_info(feeder)


class FeederRecordsEntity(CoordinatorEntity[FeederRecordsCoordinator]):
    """Entity backed by the cloud records."""

    _attr_has_entity_name = True

    def __init__(self, feeder: Feeder, key: str) -> None:
        """Initialize."""
        super().__init__(feeder.records)
        self.feeder = feeder
        self._attr_unique_id = f"{feeder.did}_{key}"
        self._attr_device_info = device_info(feeder)
