"""Data update coordinators for the Xiaomi Smart Pet Feeder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Desiccant, FeederApi, FeederStatus, FeedRecord, XiaomiAuthError, XiaomiError
from .const import (
    CONF_RECORDS_INTERVAL,
    CONF_STATUS_INTERVAL,
    CONSUMABLES_INTERVAL,
    DEFAULT_RECORDS_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DOMAIN,
)

if TYPE_CHECKING:
    from .data import XiaomiPetFeederConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FeederRecords:
    """Cloud side data: what the app shows from its eco service."""

    eaten_today: int
    today: list[FeedRecord]
    new: list[FeedRecord] = field(default_factory=list)
    desiccant: Desiccant | None = None


class FeederStatusCoordinator(DataUpdateCoordinator[FeederStatus]):
    """Polls the MIoT properties of one feeder."""

    config_entry: XiaomiPetFeederConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: XiaomiPetFeederConfigEntry,
        api: FeederApi,
        name: str,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{name} status",
            update_interval=timedelta(
                seconds=entry.options.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL)
            ),
        )
        self.api = api

    async def _async_update_data(self) -> FeederStatus:
        try:
            status = await self.api.status()
        except XiaomiAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_expired"
            ) from err
        except XiaomiError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if not status.values:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="device_offline")
        return status

    def apply(self, prop: tuple[int, int], value: Any) -> None:
        """Optimistically store a value we just wrote."""
        if self.data is None:
            return
        values = dict(self.data.values)
        values[prop] = value
        self.async_set_updated_data(FeederStatus(values))


class FeederRecordsCoordinator(DataUpdateCoordinator[FeederRecords]):
    """Polls the eco service: food eaten today, today's log and the desiccant."""

    config_entry: XiaomiPetFeederConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: XiaomiPetFeederConfigEntry,
        api: FeederApi,
        status: FeederStatusCoordinator,
        name: str,
        home: tuple[int, int] | None,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{name} records",
            update_interval=timedelta(
                seconds=entry.options.get(CONF_RECORDS_INTERVAL, DEFAULT_RECORDS_INTERVAL)
            ),
        )
        self.api = api
        self.status = status
        self._home = home
        self._seen: set[tuple[int, str]] | None = None
        self._desiccant: Desiccant | None = None
        self._desiccant_at: datetime | None = None
        self._pending: list[CALLBACK_TYPE] = []

    @callback
    def refresh_soon(self, *delays: timedelta) -> None:
        """Schedule refreshes to pick up records uploaded after a command."""

        async def _refresh(_now: datetime) -> None:
            await self.async_request_refresh()

        self._pending = [
            *self._pending[-8:],
            *(async_call_later(self.hass, delay, _refresh) for delay in delays),
        ]

    async def async_shutdown(self) -> None:
        """Cancel scheduled refreshes."""
        for cancel in self._pending:
            cancel()
        self._pending.clear()
        await super().async_shutdown()

    @property
    def tz_offset(self) -> int:
        """Device timezone offset in seconds."""
        return self.status.data.tz_offset if self.status.data else 0

    @property
    def dst(self) -> bool:
        """Device DST flag."""
        return self.status.data.dst if self.status.data else False

    def invalidate_desiccant(self) -> None:
        """Force the desiccant to be fetched on the next refresh."""
        self._desiccant_at = None

    async def _async_update_data(self) -> FeederRecords:
        try:
            eaten = await self.api.eaten_today(self.tz_offset)
            today = await self.api.records_today(self.tz_offset, self.dst)
            await self._update_desiccant()
        except XiaomiAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_expired"
            ) from err
        except XiaomiError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        keys = {r.key for r in today}
        if self._seen is None:
            new: list[FeedRecord] = []
            self._seen = keys
        else:
            new = [r for r in today if r.key not in self._seen]
            # Keep yesterday's keys too so a late upload around midnight is not lost.
            self._seen = (self._seen | keys) if len(self._seen) < 2000 else keys
        return FeederRecords(eaten_today=eaten, today=today, new=new, desiccant=self._desiccant)

    async def _update_desiccant(self) -> None:
        if self._home is None:
            return
        now = dt_util.utcnow()
        if self._desiccant_at and now - self._desiccant_at < CONSUMABLES_INTERVAL:
            return
        try:
            self._desiccant = await self.api.desiccant(*self._home)
            self._desiccant_at = now
        except XiaomiAuthError:
            raise
        except XiaomiError as err:
            # Not fatal: keep the last known value and retry next cycle.
            _LOGGER.debug("Could not read consumables: %s", err)
