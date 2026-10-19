"""Real-time updates borrowed from the official Xiaomi Home integration.

Xiaomi only pushes device events to its own integration (``xiaomi_home``). When that
integration also has the feeder, its event entities change the moment the feeder reports a
meal or a dispense, so we watch their state and refresh right away instead of waiting for
the next poll.

We never subscribe to the official MIoT client ourselves: ``MIoTClient.sub_event`` keeps a
single handler per event, so it would silently steal the events from the official entity.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import slugify

from .api import SUPPORTED_MODELS

if TYPE_CHECKING:
    from .data import Feeder

_LOGGER = logging.getLogger(__name__)

OFFICIAL_DOMAIN = "xiaomi_home"

# Spec events of the pet-feeder service (siid 2) that end up in the cloud records:
# eiid 1 is day-eaten-food (a meal), eiid 3 and 5 are dispense results (food-out-complete).
# The official entity ids end in ``_e_<siid>_<eiid>``; names and attributes are translated.
OFFICIAL_EVENTS = ((2, 1), (2, 3), (2, 5))
_SUFFIXES = tuple(f"_e_{siid}_{eiid}" for siid, eiid in OFFICIAL_EVENTS)

# The push can arrive before the eco service has stored the record: check again shortly.
FOLLOW_UP_REFRESH = timedelta(seconds=20)

_NO_EVENT = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _is_official_device(device: dr.DeviceEntry, did: str) -> bool:
    """Whether a device of the official integration is this feeder.

    The official identifier is ``<cloud server>_<did>`` slugified, e.g. ``de_123456``.
    """
    return device.model in SUPPORTED_MODELS and any(
        domain == OFFICIAL_DOMAIN and ident.partition("_")[2] == slugify(did)
        for domain, ident in device.identifiers
    )


@callback
def official_event_entities(hass: HomeAssistant, did: str) -> list[str]:
    """Return the enabled event entities of the official integration for one feeder."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    found = [
        entity.entity_id
        for official in hass.config_entries.async_entries(OFFICIAL_DOMAIN)
        for device in dr.async_entries_for_config_entry(device_registry, official.entry_id)
        if _is_official_device(device, did)
        for entity in er.async_entries_for_device(entity_registry, device.id)
        if entity.platform == OFFICIAL_DOMAIN
        and entity.domain == Platform.EVENT
        and entity.unique_id.endswith(_SUFFIXES)
    ]
    return sorted(found)


class OfficialEventListener:
    """Refresh one feeder whenever the official integration reports one of its events."""

    def __init__(self, hass: HomeAssistant, feeder: Feeder) -> None:
        """Initialize."""
        self._hass = hass
        self._feeder = feeder
        self.entity_ids: list[str] = []
        self._last: dict[str, str] = {}
        self._unsub_state: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start listening; the official entities may also appear or change later."""
        self._track()
        unsub_registry = self._hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            self._registry_updated,
            event_filter=self._registry_filter,
        )

        @callback
        def stop() -> None:
            unsub_registry()
            self._untrack()

        return stop

    @callback
    def _track(self) -> None:
        entity_ids = official_event_entities(self._hass, self._feeder.did)
        if entity_ids == self.entity_ids:
            return
        self._untrack()
        self.entity_ids = entity_ids
        self._last = {
            entity_id: state.state
            for entity_id in entity_ids
            if (state := self._hass.states.get(entity_id)) and state.state not in _NO_EVENT
        }
        if entity_ids:
            _LOGGER.debug("%s: real-time updates from %s", self._feeder.name, entity_ids)
            self._unsub_state = async_track_state_change_event(
                self._hass, entity_ids, self._state_changed
            )

    @callback
    def _untrack(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self.entity_ids = []

    @callback
    def _registry_filter(self, data: er.EventEntityRegistryUpdatedData) -> bool:
        """Only official event entities, or ones we track (renamed or removed)."""
        if data["entity_id"] in self.entity_ids:
            return True
        if data["action"] == "update" and data.get("old_entity_id") in self.entity_ids:
            return True
        entity = er.async_get(self._hass).async_get(data["entity_id"])
        return (
            entity is not None
            and entity.platform == OFFICIAL_DOMAIN
            and entity.domain == Platform.EVENT
        )

    @callback
    def _registry_updated(self, _event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        self._track()

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if new is None or new.state in _NO_EVENT:
            return
        # An event entity's state is the time of its last event. Being added, restored or
        # coming back online writes an old time again, which is not a new event.
        last = self._last.get(new.entity_id)
        self._last[new.entity_id] = new.state
        if old is None or old.state == STATE_UNAVAILABLE or new.state == last:
            return
        _LOGGER.debug("%s: event pushed by %s", self._feeder.name, new.entity_id)
        feeder = self._feeder
        self._hass.async_create_task(feeder.records.async_request_refresh(), eager_start=True)
        self._hass.async_create_task(feeder.status.async_request_refresh(), eager_start=True)
        feeder.records.refresh_soon(FOLLOW_UP_REFRESH)
