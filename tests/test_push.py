"""Real-time updates from the event entities of the official Xiaomi Home integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.xiaomi_pet_feeder.const import (
    CONF_OFFICIAL_PUSH,
    CONF_RECORDS_INTERVAL,
    CONF_STATUS_INTERVAL,
    DOMAIN,
)
from custom_components.xiaomi_pet_feeder.push import OFFICIAL_DOMAIN, official_event_entities

from .conftest import DID, FakeCloud

EATEN = "/eco/pet-feeder-new/petFeederEventApi/getHasEatRecord"
PROP_GET = "/miotspec/prop/get"
T1 = "2026-10-19T08:00:00.000+00:00"
T2 = "2026-10-19T08:05:00.000+00:00"


def official_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry of the official integration."""
    entries = hass.config_entries.async_entries(OFFICIAL_DOMAIN)
    if entries:
        return entries[0]
    official = MockConfigEntry(domain=OFFICIAL_DOMAIN, title="Xiaomi Home")
    official.add_to_hass(hass)
    return official


def add_official(
    hass: HomeAssistant,
    did: str = DID,
    model: str = "xiaomi.feeder.iv2001",
    kinds: tuple[str, ...] = ("e_2_1", "e_2_3", "e_2_5"),
    **kwargs: object,
) -> list[str]:
    """Register the feeder and its entities the way the official integration does."""
    official = official_entry(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=official.entry_id,
        identifiers={(OFFICIAL_DOMAIN, f"us_{did}")},
        model=model,
        name="Official feeder",
    )
    entity_registry = er.async_get(hass)
    entity_ids = []
    for kind in kinds:
        platform = "event" if kind.startswith("e_") else "sensor"
        entity = entity_registry.async_get_or_create(
            platform,
            OFFICIAL_DOMAIN,
            f"xiaomi_home.xiaomi_us_{did}_iv2001_pet_food_{kind}",
            config_entry=official,
            device_id=device.id,
            suggested_object_id=f"xiaomi_us_{did}_iv2001_{kind}",
            **kwargs,
        )
        entity_ids.append(entity.entity_id)
    return entity_ids


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Load the entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def push(hass: HomeAssistant, entity_id: str, state: str) -> None:
    """Change the state of an official event entity."""
    hass.states.async_set(entity_id, state, {"event_type": "Pet Food Out"})
    await hass.async_block_till_done()


def polls(cloud: FakeCloud) -> tuple[int, int]:
    """Records and status reads so far."""
    return len(cloud.calls_to(EATEN)), len(cloud.calls_to(PROP_GET))


async def test_official_event_refreshes_now(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    cloud: FakeCloud,
    hass_client: ClientSessionGenerator,
) -> None:
    """A meal pushed to the official entity shows up without waiting for the poll."""
    meal_done, *_ = add_official(hass)
    hass.states.async_set(meal_done, T1)
    await setup(hass, entry)
    assert entry.runtime_data.feeders[DID].push.entity_ids == sorted(
        official_event_entities(hass, DID)
    )
    meal = er.async_get(hass).async_get_entity_id("event", DOMAIN, f"{DID}_meal_event")
    rows = cloud.responses["feed_record"]["result"]
    rows.append(
        {
            "timeZoneOffSet": -10800,
            "ts": rows[-1]["ts"] + 60,
            "siid": 2,
            "eiid": 1,
            "params": [{"piid": 18, "value": "9"}],
        }
    )
    before = polls(cloud)

    await push(hass, meal_done, T2)

    assert polls(cloud) == (before[0] + 1, before[1] + 1)
    assert hass.states.get(meal).attributes["grams"] == 9

    # One more look shortly after, in case the cloud stored the record late.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=21))
    await hass.async_block_till_done()
    assert polls(cloud)[0] == before[0] + 2

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["feeders"][0]["official_events"] == 3


async def test_first_event_counts(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """An entity that never had an event (unknown) still triggers on its first one."""
    *_, dispensed = add_official(hass)
    hass.states.async_set(dispensed, STATE_UNKNOWN)
    await setup(hass, entry)
    before = polls(cloud)
    await push(hass, dispensed, T1)
    assert polls(cloud)[0] == before[0] + 1


async def test_restore_and_availability_are_not_events(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """Being added, restored, or going offline and back does not refresh."""
    meal_done, *_ = add_official(hass)
    await setup(hass, entry)
    before = polls(cloud)

    await push(hass, meal_done, T1)  # first state written: entity added or restored
    await push(hass, meal_done, STATE_UNAVAILABLE)
    await push(hass, meal_done, T1)
    await push(hass, meal_done, STATE_UNKNOWN)
    hass.states.async_set(meal_done, T1, {"event_type": "other attributes only"})
    await hass.async_block_till_done()

    assert polls(cloud) == before


async def test_only_matching_official_entities(hass: HomeAssistant) -> None:
    """Same did and a supported model, enabled event entities with the right ids."""
    add_official(hass, kinds=("e_2_1", "e_2_2", "e_3_1", "p_2_1"))
    add_official(hass, kinds=("e_2_5",), disabled_by=er.RegistryEntryDisabler.USER)
    add_official(hass, did=f"9{DID}")  # did that only ends like ours
    add_official(hass, did="777", model="xiaomi.feeder.other")
    assert official_event_entities(hass, DID) == [f"event.xiaomi_us_{DID}_iv2001_e_2_1"]
    assert official_event_entities(hass, "777") == []


async def test_official_installed_later(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """Entities registered after setup, renamed or removed are followed."""
    await setup(hass, entry)
    listener = entry.runtime_data.feeders[DID].push
    assert listener.entity_ids == []

    meal_done, *_ = add_official(hass)
    await hass.async_block_till_done()
    assert meal_done in listener.entity_ids
    hass.states.async_set(meal_done, T1)
    before = polls(cloud)
    await push(hass, meal_done, T2)
    assert polls(cloud)[0] == before[0] + 1

    entity_registry = er.async_get(hass)
    entity_registry.async_update_entity(meal_done, new_entity_id="event.cat_ate")
    await hass.async_block_till_done()
    assert "event.cat_ate" in listener.entity_ids
    assert meal_done not in listener.entity_ids

    entity_registry.async_remove("event.cat_ate")
    await hass.async_block_till_done()
    assert "event.cat_ate" not in listener.entity_ids
    assert len(listener.entity_ids) == 2


async def test_option_off(hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud) -> None:
    """With the option off the official entities are ignored."""
    meal_done, *_ = add_official(hass)
    hass.states.async_set(meal_done, T1)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_OFFICIAL_PUSH: False})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.feeders[DID].push is None
    before = polls(cloud)
    await push(hass, meal_done, T2)
    assert polls(cloud) == before


async def test_unload_stops_listening(
    hass: HomeAssistant, entry: MockConfigEntry, cloud: FakeCloud
) -> None:
    """Nothing is refreshed once the entry is unloaded."""
    meal_done, *_ = add_official(hass)
    hass.states.async_set(meal_done, T1)
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    before = polls(cloud)
    await push(hass, meal_done, T2)
    add_official(hass, kinds=("e_2_3",))
    await hass.async_block_till_done()
    assert polls(cloud) == before


async def test_options_show_linked_feeders(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The options form says whether the official integration has the feeder."""
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"] == {"linked": "0", "total": "1"}
    hass.config_entries.options.async_abort(result["flow_id"])

    add_official(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"] == {"linked": "1", "total": "1"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_STATUS_INTERVAL: 30, CONF_RECORDS_INTERVAL: 300, CONF_OFFICIAL_PUSH: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_OFFICIAL_PUSH] is False
