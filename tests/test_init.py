"""Setup, entities, commands and services against the fake cloud."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.xiaomi_pet_feeder.api import XiaomiAuthError
from custom_components.xiaomi_pet_feeder.const import DOMAIN

from .conftest import DID, FakeCloud

ACTION = "/miotspec/action"
PROP_SET = "/miotspec/prop/set"


def eid(hass: HomeAssistant, platform: str, key: str) -> str:
    """Entity id from the unique id."""
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DID}_{key}")
    assert entity_id, f"{platform}.{key} missing"
    return entity_id


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Load the entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def actions(cloud: FakeCloud) -> list[dict[str, Any]]:
    """Action payloads sent so far."""
    return [c["params"] for c in cloud.calls_to(ACTION)]


async def test_entities_match_the_app(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """Values come from the real responses captured from a feeder."""
    await setup(hass, entry)

    def state(platform: str, key: str) -> str:
        return hass.states.get(eid(hass, platform, key)).state

    assert state("sensor", "eaten_today") == "12"
    assert state("sensor", "bowl_food") == "0"
    assert state("sensor", "schedule_progress") == "25"
    assert state("sensor", "status") == "food_low"
    assert state("sensor", "desiccant") == "exhausted"
    assert state("sensor", "feeding_log") == "4"
    log = hass.states.get(eid(hass, "sensor", "feeding_log")).attributes["records"]
    assert log[0]["type"] == "meal"

    assert state("binary_sensor", "food_low") == "on"
    assert state("binary_sensor", "food_stuck") == "off"
    assert state("binary_sensor", "dispensing") == "off"

    assert state("switch", "auto_feed") == "on"
    assert state("switch", "refill_reminder") == "on"
    assert state("switch", "child_lock") == "off"
    assert state("switch", "dispense_compensation") == "on"
    assert state("number", "low_intake_threshold") == "40"
    assert state("number", "empty_bowl_duration") == "8"
    assert state("number", "portions") == "1"
    assert state("select", "screen_display") == "bowl_food"

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, DID), entry.entry_id)
    assert device is not None
    assert device.sw_version == "2.2.3_0027"
    assert device.model_id == "xiaomi.feeder.iv2001"

    # Second query with the DST-shifted offset, like plugin 1.1.x.
    offsets = {
        c["timeZoneOffSetSeconds"]
        for c in cloud.calls_to("/eco/pet-feeder-new/petFeederEventApi/getFeedRecord")
    }
    assert offsets == {-10800, -14400}  # 5.19=1: DST, so also standard time


async def test_dispense(hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud) -> None:
    """Dispense uses action 2.1 with portions, never property 2.7."""
    await setup(hass, entry)
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: eid(hass, "number", "portions"), "value": 3},
        blocking=True,
    )
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: eid(hass, "button", "dispense")}, blocking=True
    )
    assert actions(cloud)[-1] == {"did": DID, "siid": 2, "aiid": 1, "in": [3]}
    assert not any(p.get("piid") == 7 for c in cloud.calls_to(PROP_SET) for p in c["params"])
    # Follow-up refreshes are cancelled when the entry unloads.
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_dispense_refused_when_jammed(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """The app refuses to dispense while food is jammed."""
    cloud.set_prop(2, 10, 1)
    await setup(hass, entry)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, DID), entry.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "dispense", {"device_id": device.id, "portions": 1}, blocking=True
        )
    assert actions(cloud) == []


async def test_settings_write_like_the_app(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """Property writes vs actions, with the companion values the app sends."""
    await setup(hass, entry)

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: eid(hass, "switch", "child_lock")}, blocking=True
    )
    assert cloud.calls_to(PROP_SET)[-1]["params"] == [
        {"did": DID, "siid": 3, "piid": 1, "value": True}
    ]

    await hass.services.async_call(
        "switch",
        "turn_off",
        {ATTR_ENTITY_ID: eid(hass, "switch", "prevent_spillage")},
        blocking=True,
    )
    assert cloud.calls_to(PROP_SET)[-1]["params"][0]["value"] == 0

    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: eid(hass, "number", "low_intake_threshold"), "value": 50},
        blocking=True,
    )
    assert actions(cloud)[-1] == {"did": DID, "siid": 5, "aiid": 1, "in": [50, False]}

    await hass.services.async_call(
        "switch",
        "turn_off",
        {ATTR_ENTITY_ID: eid(hass, "switch", "refill_reminder")},
        blocking=True,
    )
    assert actions(cloud)[-1] == {"did": DID, "siid": 5, "aiid": 3, "in": [False, 8]}

    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: eid(hass, "select", "screen_display"), "option": "eaten_today"},
        blocking=True,
    )
    assert cloud.calls_to(PROP_SET)[-1]["params"][0] == {
        "did": DID,
        "siid": 5,
        "piid": 18,
        "value": 1,
    }

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: eid(hass, "switch", "auto_feed")}, blocking=True
    )
    assert actions(cloud)[-1] == {
        "did": DID,
        "siid": 5,
        "aiid": 2,
        "in": ["[0,06000101,10000101,16000101,20000101]"],
    }


async def test_schedule_services(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """Schedule edits go through action 5.2 and are validated first."""
    await setup(hass, entry)
    device_id = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, DID), entry.entry_id).id

    await hass.services.async_call(
        DOMAIN,
        "add_schedule_entry",
        {"device_id": device_id, "time": "21:30", "portions": 2},
        blocking=True,
    )
    written = actions(cloud)[-1]["in"][0]
    assert written.startswith("[1,06000101,10000101,16000101,20000101,2130020")

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {
                "device_id": device_id,
                "entries": [{"time": "08:00", "portions": 1}, {"time": "08:02", "portions": 1}],
            },
            blocking=True,
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "remove_schedule_entry",
            {"device_id": device_id, "time": "07:00"},
            blocking=True,
        )

    schedule = await hass.services.async_call(
        DOMAIN, "get_schedule", {"device_id": device_id}, blocking=True, return_response=True
    )
    assert schedule["auto_feed"] is True
    assert [e["time"] for e in schedule["entries"]][:2] == ["06:00", "10:00"]

    hourly = await hass.services.async_call(
        DOMAIN, "get_hourly_stats", {"device_id": device_id}, blocking=True, return_response=True
    )
    assert hourly["eaten_grams"] == {1: 4, 6: 8}

    history = await hass.services.async_call(
        DOMAIN,
        "get_feeding_history",
        {"device_id": device_id, "days": 90},
        blocking=True,
        return_response=True,
    )
    assert history["records"]


async def test_new_records_fire_events(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """Only records that appear after setup trigger events."""
    await setup(hass, entry)
    meal = eid(hass, "event", "meal_event")
    assert hass.states.get(meal).state == "unknown"

    rows = cloud.responses["feed_record"]["result"]
    rows.append(
        {
            "timeZoneOffSet": -10800,
            "ts": rows[-1]["ts"] + 60,
            "siid": 2,
            "eiid": 1,
            "params": [{"piid": 18, "value": "7"}],
        }
    )
    await entry.runtime_data.feeders[DID].records.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(meal)
    assert state.attributes["event_type"] == "ate"
    assert state.attributes["grams"] == 7


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """An expired session that cannot be renewed asks the user to log in again."""
    cloud.overrides["/miotspec/prop/get"] = {"code": 3, "message": "auth err"}
    entry.add_to_hass(hass)
    with patch(
        "custom_components.xiaomi_pet_feeder.api.auth.XiaomiAuthenticator.refresh",
        side_effect=XiaomiAuthError("expired"),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_diagnostics_are_redacted(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    cloud: FakeCloud,
    hass_client: ClientSessionGenerator,
) -> None:
    """No credentials or identifiers in diagnostics."""
    await setup(hass, entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = str(diag)
    for secret in ("c3NlY3VyaXR5", "token", "user@example.com", DID):
        assert secret not in text or (secret == "token" and "service_token" not in text)
    assert diag["feeders"][0]["eaten_today"] == 12


async def test_unload(hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud) -> None:
    """The entry unloads cleanly."""
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
