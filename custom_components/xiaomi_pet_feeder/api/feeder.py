"""High level API of the Xiaomi Smart Pet Food Feeder 2 (iv2001 / pi2001).

Property and action ids come from the MIoT spec and from the official Mi Home
plugin (``com.midr.pet.feeder``), which is what the app actually uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from enum import StrEnum
from typing import Any

from .client import XiaomiCloudClient
from .schedule import GRAMS_PER_PORTION, FeedingSchedule

SUPPORTED_MODELS = ("xiaomi.feeder.iv2001", "xiaomi.feeder.pi2001")

ECO_API = "/eco/pet-feeder-new/petFeederEventApi"
ACCESS_KEY = "IOS00026747c5acafc2"


class Prop:
    """(siid, piid) of the properties used by the app."""

    FIRMWARE = (1, 4)
    SENSOR_FAULT = (2, 1)
    FOOD_LOW = (2, 6)
    FOOD_STUCK = (2, 10)
    BOWL_ABNORMAL = (2, 11)
    REFILL_DUE = (2, 13)
    BOWL_HEAP = (2, 15)
    BOWL_WEIGHT = (2, 22)
    DISPENSING = (2, 26)
    LOW_INTAKE = (2, 32)
    CHILD_LOCK = (3, 1)
    AUTO_SCREEN_OFF = (3, 3)
    BATTERY_LOW = (4, 1)
    SCHEDULE = (5, 1)
    REFILL_REMINDER = (5, 3)
    SCHEDULE_DISPLAY = (5, 4)
    LOW_INTAKE_THRESHOLD = (5, 5)
    LOW_INTAKE_NOTIFY = (5, 6)
    EMPTY_BOWL_HOURS = (5, 10)
    DISPENSE_PROGRESS = (5, 11)
    COMPENSATION = (5, 12)
    PREVENT_HEAP = (5, 14)
    SCHEDULE_PROGRESS = (5, 15)
    REFILL_TRIGGER_HOURS = (5, 16)
    TIMEZONE = (5, 17)
    SCREEN_DISPLAY = (5, 18)
    DST = (5, 19)


POLLED_PROPS: list[tuple[int, int]] = [
    v for k, v in vars(Prop).items() if not k.startswith("_") and isinstance(v, tuple)
]


class Action:
    """(siid, aiid) of the actions used by the app."""

    DISPENSE = (2, 1)  # in: [portions]
    CALIBRATE = (2, 2)  # in: []
    LOW_INTAKE_SETTINGS = (5, 1)  # in: [threshold %, notify bool]
    WRITE_SCHEDULE = (5, 2)  # in: [schedule string]
    REFILL_SETTINGS = (5, 3)  # in: [reminder bool, empty bowl hours]
    SCHEDULE_DISPLAY = (5, 4)  # in: [bool]
    CLEAR_LOW_INTAKE = (5, 5)  # in: [0]
    CLEAR_REFILL = (5, 6)  # in: [0]
    RESET_DESICCANT = (6, 1)  # in: []


class FeedSource(StrEnum):
    """Who triggered a dispense (units digit of event piid 16)."""

    APP = "app"
    BUTTON = "button"
    SCHEDULE = "schedule"
    EAT = "eat"
    UNKNOWN = "unknown"


class FeedResult(StrEnum):
    """Outcome of a dispense (tens digit of event piid 16)."""

    OK = "ok"
    FOOD_STUCK = "food_stuck"
    OUT_OF_FOOD = "out_of_food"
    BOWL_FULL = "bowl_full"
    OTA = "interrupted_by_update"
    FAILED = "failed"
    UNKNOWN = "unknown"


_SOURCES = {0: FeedSource.APP, 1: FeedSource.BUTTON, 2: FeedSource.SCHEDULE, 3: FeedSource.EAT}
_RESULTS = {
    0: FeedResult.OK,
    1: FeedResult.FOOD_STUCK,
    2: FeedResult.OUT_OF_FOOD,
    3: FeedResult.BOWL_FULL,
    4: FeedResult.OTA,
    5: FeedResult.FAILED,
}


class DesiccantState(StrEnum):
    """Desiccant cartridge state as shown by the app."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


_DESICCANT = {
    1: DesiccantState.SUFFICIENT,
    3: DesiccantState.INSUFFICIENT,
    4: DesiccantState.EXHAUSTED,
}


@dataclass(frozen=True, slots=True)
class FeedRecord:
    """A meal (pet ate) or a dispense, as stored in the cloud."""

    ts: int
    kind: str  # "meal" | "dispense"
    grams: int | None = None
    portions: int | None = None
    source: FeedSource | None = None
    result: FeedResult | None = None

    @property
    def time(self) -> dt.datetime:
        """UTC timestamp."""
        return dt.datetime.fromtimestamp(self.ts, dt.UTC)

    def as_dict(self) -> dict[str, Any]:
        """Serializable form for attributes and service responses."""
        data: dict[str, Any] = {"time": self.time.isoformat(), "type": self.kind}
        if self.kind == "meal":
            data["grams"] = self.grams
        else:
            data |= {
                "portions": self.portions,
                "source": self.source,
                "result": self.result,
            }
        return data

    @property
    def key(self) -> tuple[int, str]:
        """Identity used to detect new records."""
        return (self.ts, self.kind)


def parse_record(row: dict[str, Any]) -> FeedRecord | None:
    """Turn a petFeederEventApi row into a record; unknown events are dropped."""
    params = {p.get("piid"): p.get("value") for p in row.get("params") or []}
    ts = int(row.get("ts") or 0)
    eiid = row.get("eiid")
    try:
        if eiid == 1:
            return FeedRecord(ts=ts, kind="meal", grams=int(params.get(18) or 0))
        if eiid in (3, 5):
            code = int(params.get(16) or 0)
            amount = int(params.get(8) or 0)
            return FeedRecord(
                ts=ts,
                kind="dispense",
                portions=round(amount / GRAMS_PER_PORTION),
                source=_SOURCES.get(code % 10, FeedSource.UNKNOWN),
                result=_RESULTS.get(code // 10, FeedResult.UNKNOWN),
            )
    except TypeError, ValueError:
        return None
    return None


@dataclass(slots=True)
class Desiccant:
    """Desiccant cartridge info from the consumables API."""

    state: DesiccantState
    level: int | None = None
    hours_left: int | None = None
    total_hours: int | None = None


@dataclass(slots=True)
class FeederStatus:
    """Snapshot of the MIoT properties."""

    values: dict[tuple[int, int], Any] = field(default_factory=dict)

    def get(self, prop: tuple[int, int], default: Any = None) -> Any:
        """Raw property value."""
        return self.values.get(prop, default)

    def flag(self, prop: tuple[int, int]) -> bool | None:
        """Property as a boolean (0/1 or bool)."""
        value = self.values.get(prop)
        return None if value is None else bool(int(value))

    @property
    def schedule(self) -> FeedingSchedule:
        """Parsed feeding plan."""
        return FeedingSchedule.parse(self.values.get(Prop.SCHEDULE))

    @property
    def tz_offset(self) -> int:
        """Device UTC offset in seconds (falls back to UTC)."""
        value = self.values.get(Prop.TIMEZONE)
        return int(value) if isinstance(value, int | float) else 0

    @property
    def dst(self) -> bool:
        """Whether the device reports daylight saving time (5.19)."""
        return bool(self.values.get(Prop.DST))


def device_now(tz_offset: int) -> dt.datetime:
    """Return the current wall time of the device as a naive datetime."""
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=tz_offset)).replace(tzinfo=None)


def device_midnight(tz_offset: int, day: dt.date | None = None) -> int:
    """Unix timestamp of 00:00 device time for ``day`` (default: today)."""
    day = day or device_now(tz_offset).date()
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)
    return int(midnight.timestamp()) - tz_offset


class FeederApi:
    """Operations on one feeder."""

    def __init__(self, client: XiaomiCloudClient, did: str, owner_id: str) -> None:
        """Bind to a device; ``owner_id`` is the Xiaomi id of the device owner."""
        self.client = client
        self.did = did
        self.owner_id = owner_id

    async def status(self) -> FeederStatus:
        """Read every property the integration exposes."""
        return FeederStatus(await self.client.get_properties(self.did, POLLED_PROPS))

    async def set_property(self, prop: tuple[int, int], value: Any) -> None:
        """Write a property."""
        await self.client.set_property(self.did, prop[0], prop[1], value)

    async def action(self, action: tuple[int, int], *ins: Any) -> None:
        """Run an action."""
        await self.client.action(self.did, action[0], action[1], list(ins))

    # -------------------------------------------------------- eco service
    def _eco_base(self, tz_offset: int) -> dict[str, Any]:
        return {
            "userId": self.owner_id,
            "deviceId": self.did,
            "timeZoneOffSetSeconds": tz_offset,
            "accessKey": ACCESS_KEY,
        }

    async def eaten_today(self, tz_offset: int) -> int:
        """Grams eaten since device midnight (the app's "Food eaten today")."""
        result = await self.client.request(
            f"{ECO_API}/getHasEatRecord",
            self._eco_base(tz_offset) | {"startTime": device_midnight(tz_offset)},
        )
        return int(result or 0)

    @staticmethod
    def _offsets(tz_offset: int, dst: bool) -> tuple[int, int]:
        """Offsets queried by plugin 1.1.x: the device one and the DST-shifted one.

        Records are stored with the offset in force when they happened, so around
        DST changes the app asks twice and merges the answers.
        """
        return tz_offset, tz_offset + (-3600 if dst else 3600)

    async def records_today(self, tz_offset: int, dst: bool = False) -> list[FeedRecord]:
        """Meals and dispenses of today, oldest first."""
        rows: list[dict[str, Any]] = []
        for offset in self._offsets(tz_offset, dst):
            start = device_midnight(tz_offset)
            for _ in range(10):
                page = await self.client.request(
                    f"{ECO_API}/getFeedRecord",
                    self._eco_base(offset) | {"startTime": start, "pageCount": 60},
                )
                page = page if isinstance(page, list) else []
                rows.extend(page)
                if len(page) < 60:
                    break
                start = int(page[-1].get("ts") or start)
        return _dedupe(rows)

    async def records_between(
        self,
        tz_offset: int,
        start_ts: int,
        end_ts: int,
        dst: bool = False,
        limit: int = 2000,
    ) -> list[FeedRecord]:
        """History between two timestamps (the cloud keeps 90 days), newest first."""
        rows: list[dict[str, Any]] = []
        for offset in self._offsets(tz_offset, dst):
            cursor = end_ts
            fetched = 0
            while fetched < limit:
                page = await self.client.request(
                    f"{ECO_API}/multiGetFeedRecord",
                    self._eco_base(offset) | {"startTime": cursor, "pageCount": 400},
                )
                page = page if isinstance(page, list) else []
                fetched += len(page)
                rows.extend(r for r in page if start_ts <= int(r.get("ts") or 0) <= end_ts)
                if len(page) < 400 or int(page[-1].get("ts") or 0) < start_ts:
                    break
                cursor = int(page[-1]["ts"])
        records = _dedupe(rows)
        records.reverse()
        return records

    async def hourly(self, tz_offset: int, day: dt.date) -> dict[str, Any]:
        """Hourly chart of one day: grams eaten and portions dispensed per hour."""
        result = await self.client.request(
            f"{ECO_API}/getHourlyRecord",
            self._eco_base(tz_offset) | {"startTime": device_midnight(tz_offset, day)},
        )
        result = result or {}
        return {
            "date": day.isoformat(),
            "eaten_grams": {
                int(r["hour"]): int(r["param"] or 0)
                for r in result.get("hourlyEatFoodRecords") or []
            },
            "dispensed_portions": {
                int(r["hour"]): round(int(r["param"] or 0) / GRAMS_PER_PORTION)
                for r in result.get("hourlyOutFoodRecords") or []
            },
            "low_intake_percent": result.get("lowEatFoodParam"),
        }

    async def daily(self, tz_offset: int, start: dt.date, end: dt.date) -> dict[str, Any]:
        """Daily chart: grams eaten and food left in the bowl at midnight."""
        result = await self.client.request(
            f"{ECO_API}/getDailyRecord",
            self._eco_base(tz_offset)
            | {
                "startTime": device_midnight(tz_offset, start),
                "endTime": device_midnight(tz_offset, end) + 86399,
            },
        )
        result = result or {}
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "eaten_grams": {
                r["date"]: int(r["param"] or 0) for r in result.get("dailyEatFoodRecords") or []
            },
            "left_in_bowl_grams": {
                r["date"]: int(r["param"] or 0) for r in result.get("dailyRestFoodRecords") or []
            },
        }

    async def desiccant(self, home_id: int, home_owner: int) -> Desiccant | None:
        """Desiccant state from the consumables API (MIoT 6.1/6.2 always read 0)."""
        result = await self.client.request(
            "/v2/home/standard_consumable_items",
            {
                "home_id": int(home_id),
                "owner_id": int(home_owner),
                "dids": [],
                "accessKey": ACCESS_KEY,
                "filter_ignore": True,
            },
        )
        for item in (result or {}).get("items") or []:
            for device in item.get("consumes_data") or []:
                if str(device.get("did")) != self.did:
                    continue
                for detail in device.get("details") or []:
                    if detail.get("prop") == "prop.6.1":
                        return Desiccant(
                            state=_DESICCANT.get(detail.get("state"), DesiccantState.UNKNOWN),
                            level=_int_or_none(detail.get("value")),
                            hours_left=_int_or_none(detail.get("left_time")),
                            total_hours=_int_or_none(detail.get("total_life")),
                        )
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _dedupe(rows: list[dict[str, Any]]) -> list[FeedRecord]:
    seen: set[tuple[int, str]] = set()
    records: list[FeedRecord] = []
    for row in rows:
        record = parse_record(row)
        if record is None or record.key in seen:
            continue
        seen.add(record.key)
        records.append(record)
    records.sort(key=lambda r: r.ts)
    return records
