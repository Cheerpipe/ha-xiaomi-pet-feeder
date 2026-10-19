"""Tests for the API layer that do not need Home Assistant."""

from __future__ import annotations

import base64
import datetime as dt
import os

import pytest

from custom_components.xiaomi_pet_feeder.api import (
    DesiccantState,
    FeedingSchedule,
    FeedResult,
    FeedSource,
    ScheduleEntry,
    ScheduleError,
    device_midnight,
)
from custom_components.xiaomi_pet_feeder.api.crypto import (
    _rc4_keystream_xor,
    build_rc4_form,
    decrypt,
    encrypt,
    sha1_signature,
    signed_nonce,
)
from custom_components.xiaomi_pet_feeder.api.feeder import FeederApi, parse_record

from .conftest import DID, OWNER, load


def _plain_rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def test_rc4_drops_first_1024_bytes() -> None:
    """The Mi Home variant equals standard RC4 after skipping 1024 bytes."""
    assert _plain_rc4(b"Key", b"Plaintext").hex() == "bbf316e8d940af0ad3"
    key = os.urandom(32)
    data = b'{"did":"1","siid":2}'
    assert _rc4_keystream_xor(key, data) == _plain_rc4(key, bytes(1024) + data)[1024:]


def test_encrypt_roundtrip_and_signature() -> None:
    """Encryption round trips and the signature ignores the /app prefix."""
    ssecurity = base64.b64encode(os.urandom(16)).decode()
    key = signed_nonce(ssecurity, base64.b64encode(os.urandom(12)).decode())
    assert decrypt(key, encrypt(key, "hola")) == b"hola"
    assert sha1_signature("POST", "/app/home/device_list", {"a": "1"}, key) == sha1_signature(
        "post", "/home/device_list", {"a": "1"}, key
    )
    form, rkey = build_rc4_form("/app/miotspec/prop/get", '{"x":1}', ssecurity)
    assert set(form) == {"data", "rc4_hash__", "signature", "ssecurity", "_nonce"}
    assert decrypt(rkey, form["data"]) == b'{"x":1}'


def test_schedule_roundtrip() -> None:
    """The firmware string is parsed and rebuilt verbatim."""
    raw = "[1,06000101,10000101,16000101,20000101]"
    schedule = FeedingSchedule.parse(raw)
    assert schedule.auto_feed
    assert [e.time_str for e in schedule.entries] == ["06:00", "10:00", "16:00", "20:00"]
    assert all(e.portions == 1 and e.enabled for e in schedule.entries)
    assert schedule.encode() == raw
    assert FeedingSchedule.parse("[0]") == FeedingSchedule(auto_feed=False)
    assert FeedingSchedule.parse(None).entries == ()


def test_schedule_json_form() -> None:
    """The alternative JSON form accepted by the plugin."""
    schedule = FeedingSchedule.parse('{"state":1,"schedule":[{"t":28800,"v":2,"e":0}]}')
    assert schedule.auto_feed
    assert schedule.entries == (ScheduleEntry(8, 0, 2, False),)
    assert schedule.encode() == "[1,08000200]"


def test_schedule_rules() -> None:
    """The same limits as the app."""
    with pytest.raises(ScheduleError) as err:
        FeedingSchedule(True, (ScheduleEntry(8, 0, 1), ScheduleEntry(8, 3, 1))).validate()
    assert err.value.key == "schedule_too_close"
    with pytest.raises(ScheduleError):
        FeedingSchedule(True, (ScheduleEntry(23, 58, 1), ScheduleEntry(0, 1, 1))).validate()
    with pytest.raises(ScheduleError) as err:
        FeedingSchedule(True, (ScheduleEntry(8, 0, 16),)).validate()
    assert err.value.key == "schedule_invalid_portions"
    entries = tuple(ScheduleEntry(h, m, 1) for h in range(18) for m in (0, 30))
    with pytest.raises(ScheduleError) as err:
        FeedingSchedule(True, entries).validate()
    assert err.value.key == "schedule_too_many"


def test_schedule_skips_past_entries_like_the_app() -> None:
    """Entries whose time already passed today are stored as skipped."""
    schedule = FeedingSchedule(False, (ScheduleEntry(6, 0, 1), ScheduleEntry(20, 0, 1)))
    on = schedule.with_auto_feed(True, now_minutes=12 * 60)
    assert on.encode() == "[1,06000100,20000101]"
    replaced = on.with_entries([ScheduleEntry(9, 0, 3), ScheduleEntry(13, 0, 2)], 12 * 60)
    assert replaced.encode() == "[1,09000300,13000201]"


def test_parse_records_from_cloud() -> None:
    """Meals are grams, dispenses are portions x10 with a coded source/result."""
    rows = load("feed_record")["result"]
    records = [parse_record(r) for r in rows]
    meal = next(r for r in records if r and r.kind == "meal")
    dispense = next(r for r in records if r and r.kind == "dispense")
    assert meal.grams == 4
    assert dispense.portions == 1
    assert dispense.source is FeedSource.SCHEDULE
    assert dispense.result is FeedResult.OK
    jam = parse_record(
        {"ts": 1, "eiid": 5, "params": [{"piid": 16, "value": "12"}, {"piid": 8, "value": "20"}]}
    )
    assert jam is not None
    assert (jam.source, jam.result, jam.portions) == (
        FeedSource.SCHEDULE,
        FeedResult.FOOD_STUCK,
        2,
    )
    assert parse_record({"ts": 1, "eiid": 2, "params": []}) is None


def test_device_midnight() -> None:
    """Midnight is computed in device time, like the app."""
    ts = device_midnight(-10800, dt.date(2026, 10, 19))
    assert dt.datetime.fromtimestamp(ts, dt.UTC) == dt.datetime(2026, 10, 19, 3, tzinfo=dt.UTC)


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def request(self, path: str, data: dict) -> object:
        self.calls.append((path, data))
        name = {
            "/v2/home/standard_consumable_items": "consumables",
            "/eco/pet-feeder-new/petFeederEventApi/getHourlyRecord": "hourly",
            "/eco/pet-feeder-new/petFeederEventApi/getDailyRecord": "daily",
            "/eco/pet-feeder-new/petFeederEventApi/getFeedRecord": "feed_record",
        }[path]
        return load(name)["result"]


async def test_feeder_api_parsing() -> None:
    """Desiccant, charts and the DST double query."""
    client = _StubClient()
    api = FeederApi(client, DID, OWNER)  # type: ignore[arg-type]

    desiccant = await api.desiccant(1, 2)
    assert desiccant is not None
    assert desiccant.state is DesiccantState.EXHAUSTED
    assert (desiccant.level, desiccant.hours_left, desiccant.total_hours) == (0, 0, 720)

    hourly = await api.hourly(-10800, dt.date(2026, 10, 19))
    assert hourly["eaten_grams"] == {1: 4, 6: 8}
    assert hourly["dispensed_portions"] == {6: 1}

    daily = await api.daily(-10800, dt.date(2026, 10, 18), dt.date(2026, 10, 24))
    assert daily["eaten_grams"] == {"2026-10-18": 33, "2026-10-19": 12}

    client.calls.clear()
    records = await api.records_today(-10800, dst=False)
    offsets = [c[1]["timeZoneOffSetSeconds"] for c in client.calls]
    assert offsets == [-10800, -7200]
    assert len(records) == 4  # duplicates from the second query are merged
    assert [r.ts for r in records] == sorted(r.ts for r in records)
