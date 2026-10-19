"""The Xiaomi Smart Pet Feeder integration."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    FeederApi,
    XiaomiAuthenticator,
    XiaomiAuthError,
    XiaomiCloudClient,
    XiaomiCredentials,
    XiaomiError,
)
from .const import CONF_CLIENT_ID, CONF_CREDENTIALS, CONF_DEVICES, CONF_SERVER, DOMAIN
from .coordinator import FeederRecordsCoordinator, FeederStatusCoordinator
from .data import Feeder, XiaomiPetFeederConfigEntry, XiaomiPetFeederData
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: XiaomiPetFeederConfigEntry) -> bool:
    """Set up the feeders of a Xiaomi account."""
    client_id: str = entry.data[CONF_CLIENT_ID]

    async def refresh_credentials() -> XiaomiCredentials:
        """Renew the session with the stored passToken and persist it."""
        creds = client.credentials
        if not creds.pass_token:
            raise XiaomiAuthError("no passToken stored")
        session = async_create_clientsession(
            hass, cookie_jar=aiohttp.CookieJar(), auto_cleanup=False
        )
        try:
            new = await XiaomiAuthenticator(session, client_id).refresh(
                creds.user_id, creds.pass_token
            )
        finally:
            session.detach()
        if not new.pass_token:
            new.pass_token = creds.pass_token
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CREDENTIALS: new.as_dict()}
        )
        return new

    client = XiaomiCloudClient(
        async_get_clientsession(hass),
        entry.data[CONF_SERVER],
        client_id,
        XiaomiCredentials.from_dict(entry.data[CONF_CREDENTIALS]),
        refresh_credentials,
    )

    feeders: dict[str, Feeder] = {}
    for device in entry.data[CONF_DEVICES]:
        did = str(device["did"])
        api = FeederApi(client, did, str(device["owner_id"]))
        status = FeederStatusCoordinator(hass, entry, api, device["name"])
        home = (
            (int(device["home_id"]), int(device["home_owner"]))
            if device.get("home_id") and device.get("home_owner")
            else None
        )
        records = FeederRecordsCoordinator(hass, entry, api, status, device["name"], home)
        feeders[did] = Feeder(did, device["name"], device["model"], api, status, records)

    try:
        for feeder in feeders.values():
            await feeder.status.async_config_entry_first_refresh()
            await feeder.records.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        raise
    except XiaomiAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_expired"
        ) from err
    except XiaomiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = XiaomiPetFeederData(client=client, feeders=feeders)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: XiaomiPetFeederConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: XiaomiPetFeederConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a feeder that is no longer selected for this account."""
    selected = {str(d["did"]) for d in entry.data[CONF_DEVICES]}
    return not any(
        domain == DOMAIN and identifier in selected for domain, identifier in device.identifiers
    )
