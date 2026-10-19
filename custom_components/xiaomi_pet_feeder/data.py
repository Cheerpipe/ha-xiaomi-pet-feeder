"""Runtime data of the Xiaomi Smart Pet Feeder integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .api import FeederApi, XiaomiCloudClient
from .coordinator import FeederRecordsCoordinator, FeederStatusCoordinator


@dataclass(slots=True)
class Feeder:
    """One feeder and its coordinators."""

    did: str
    name: str
    model: str
    api: FeederApi
    status: FeederStatusCoordinator
    records: FeederRecordsCoordinator


@dataclass(slots=True)
class XiaomiPetFeederData:
    """Runtime data of a config entry."""

    client: XiaomiCloudClient
    feeders: dict[str, Feeder]


type XiaomiPetFeederConfigEntry = ConfigEntry[XiaomiPetFeederData]
