"""Diagnostics for the Xiaomi Smart Pet Feeder."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_CREDENTIALS
from .data import XiaomiPetFeederConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_CLIENT_ID,
    CONF_CREDENTIALS,
    "did",
    "owner_id",
    "home_id",
    "home_owner",
    "name",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: XiaomiPetFeederConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics."""
    feeders = []
    for index, feeder in enumerate(entry.runtime_data.feeders.values()):
        status = feeder.status.data
        records = feeder.records.data
        feeders.append(
            {
                "index": index,
                "model": feeder.model,
                "properties": {
                    f"{s}.{p}": value
                    for (s, p), value in (status.values.items() if status else [])
                    if (s, p) not in ((1, 3), (1, 5))  # serial numbers
                },
                "eaten_today": records.eaten_today if records else None,
                "records_today": [r.as_dict() for r in records.today] if records else None,
                "desiccant": repr(records.desiccant) if records else None,
                "status_update_ok": feeder.status.last_update_success,
                "records_update_ok": feeder.records.last_update_success,
                "official_events": len(feeder.push.entity_ids) if feeder.push else None,
            }
        )
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "feeders": feeders,
    }
