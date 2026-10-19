"""Shared fixtures: a fake Xiaomi cloud built from real (anonymized) responses."""

from __future__ import annotations

from collections.abc import Generator
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_pet_feeder.const import (
    CONF_CLIENT_ID,
    CONF_CREDENTIALS,
    CONF_DEVICES,
    CONF_SERVER,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"
DID = "100000081"
OWNER = "1000000001"
HOME = 100000000143

CREDENTIALS = {
    "user_id": OWNER,
    "ssecurity": "c3NlY3VyaXR5LXNzZWN1cml0eQ==",
    "service_token": "token",
    "pass_token": "pass",
    "c_user_id": "cuser",
}

ENTRY_DATA = {
    CONF_SERVER: "us",
    CONF_CLIENT_ID: "ABCDEFGHIJKLMNOP",
    "username": "user@example.com",
    CONF_CREDENTIALS: CREDENTIALS,
    CONF_DEVICES: [
        {
            "did": DID,
            "name": "Feeder",
            "model": "xiaomi.feeder.iv2001",
            "owner_id": OWNER,
            "home_id": HOME,
            "home_owner": int(OWNER),
        }
    ],
}


def load(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeCloud:
    """Answers XiaomiCloudClient._post like the real gateway would."""

    ROUTES = {
        "/home/device_list": "device_list",
        "/v2/homeroom/gethome": "gethome",
        "/miotspec/prop/get": "prop_get",
        "/eco/pet-feeder-new/petFeederEventApi/getHasEatRecord": "eaten_today",
        "/eco/pet-feeder-new/petFeederEventApi/getFeedRecord": "feed_record",
        "/eco/pet-feeder-new/petFeederEventApi/multiGetFeedRecord": "multi_feed_record",
        "/eco/pet-feeder-new/petFeederEventApi/getHourlyRecord": "hourly",
        "/eco/pet-feeder-new/petFeederEventApi/getDailyRecord": "daily",
        "/v2/home/standard_consumable_items": "consumables",
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.responses = {name: load(name) for name in set(self.ROUTES.values())}
        self.overrides: dict[str, Any] = {}

    def calls_to(self, path: str) -> list[Any]:
        """Payloads sent to a path."""
        return [data for p, data in self.calls if p == path]

    def set_prop(self, siid: int, piid: int, value: Any) -> None:
        """Change a property value returned by prop/get."""
        for item in self.responses["prop_get"]["result"]:
            if item["siid"] == siid and item["piid"] == piid:
                item["value"] = value

    async def post(self, _client: Any, path: str, data: Any) -> tuple[int, Any]:
        self.calls.append((path, copy.deepcopy(data)))
        if path in self.overrides:
            return 200, self.overrides[path]
        if path == "/miotspec/prop/set":
            return 200, {"code": 0, "result": [{"code": 0}]}
        if path == "/miotspec/action":
            return 200, {"code": 0, "result": {"code": 0}}
        return 200, copy.deepcopy(self.responses[self.ROUTES[path]])


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable the custom integration in every test."""
    return


@pytest.fixture
def cloud() -> Generator[FakeCloud]:
    """Patch the signed gateway client."""
    fake = FakeCloud()

    async def _post(client: Any, path: str, data: Any) -> tuple[int, Any]:
        return await fake.post(client, path, data)

    with patch(
        "custom_components.xiaomi_pet_feeder.api.client.XiaomiCloudClient._post",
        autospec=True,
        side_effect=_post,
    ):
        yield fake


@pytest.fixture
def entry() -> MockConfigEntry:
    """A configured account with one feeder."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com (us)",
        unique_id=f"{OWNER}_us",
        data=copy.deepcopy(ENTRY_DATA),
    )
