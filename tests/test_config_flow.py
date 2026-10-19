"""Config, reauth, reconfigure and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_pet_feeder.api import (
    QrChallenge,
    XiaomiCaptchaRequired,
    XiaomiCredentials,
    XiaomiInvalidCredentials,
    XiaomiVerificationRequired,
)
from custom_components.xiaomi_pet_feeder.const import (
    CONF_CREDENTIALS,
    CONF_DEVICES,
    CONF_OFFICIAL_PUSH,
    CONF_RECORDS_INTERVAL,
    CONF_STATUS_INTERVAL,
    DOMAIN,
)

from .conftest import CREDENTIALS, DID, OWNER, FakeCloud

AUTH = "custom_components.xiaomi_pet_feeder.config_flow.XiaomiAuthenticator"
CREDS = XiaomiCredentials.from_dict(CREDENTIALS)


@pytest.fixture(autouse=True)
def no_setup() -> None:
    """Do not set up the entry after the flow."""
    with patch("custom_components.xiaomi_pet_feeder.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant, method: str = "password") -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"server": "us", "login_method": method}
    )


async def test_password_with_2fa(hass: HomeAssistant, cloud: FakeCloud) -> None:
    """Password, then an e-mail code, then pick the feeder."""
    with (
        patch(f"{AUTH}.login_password", side_effect=XiaomiVerificationRequired("email")),
        patch(f"{AUTH}.submit_code", AsyncMock(return_value=CREDS)) as submit,
    ):
        result = await _start(hass)
        assert result["step_id"] == "password"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "user@example.com", "password": "secret"}
        )
        assert result["step_id"] == "verify"
        assert result["description_placeholders"] == {"method": "email"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456"}
        )
        submit.assert_awaited_once_with("123456")

    assert result["step_id"] == "devices"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICES: [DID]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == f"{OWNER}_us"
    data = result["data"]
    assert "password" not in data
    assert data[CONF_CREDENTIALS]["pass_token"] == "pass"
    assert data[CONF_DEVICES] == [
        {
            "did": DID,
            "name": "<dev_82>",
            "model": "xiaomi.feeder.iv2001",
            "owner_id": OWNER,
            "home_id": "100000000143",
            "home_owner": int(OWNER),
        }
    ]


async def test_captcha_then_login(hass: HomeAssistant, cloud: FakeCloud) -> None:
    """A captcha is shown as an image and retried."""
    with (
        patch(f"{AUTH}.login_password", side_effect=XiaomiCaptchaRequired(b"img")),
        patch(f"{AUTH}.submit_captcha", AsyncMock(return_value=CREDS)),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
        assert result["step_id"] == "captcha"
        assert result["description_placeholders"] == {"captcha_image": "aW1n"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"captcha": "abcd"}
        )
    assert result["step_id"] == "devices"


async def test_invalid_password(hass: HomeAssistant, cloud: FakeCloud) -> None:
    """Wrong credentials keep the form open with an error."""
    with patch(f"{AUTH}.login_password", side_effect=XiaomiInvalidCredentials("no")):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_qr_login(hass: HomeAssistant, cloud: FakeCloud) -> None:
    """The QR code is shown while waiting for the app."""
    challenge = QrChallenge(
        image=b"png", login_url="https://x", poll_url="https://lp", expires_at=0
    )
    with (
        patch(f"{AUTH}.start_qr", AsyncMock(return_value=challenge)),
        patch(f"{AUTH}.wait_qr", AsyncMock(return_value=CREDS)),
    ):
        result = await _start(hass, "qr")
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["description_placeholders"]["login_url"] == "https://x"
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


async def test_already_configured(
    hass: HomeAssistant, cloud: FakeCloud, entry: MockConfigEntry
) -> None:
    """The same account and region cannot be added twice."""
    entry.add_to_hass(hass)
    with patch(f"{AUTH}.login_password", AsyncMock(return_value=CREDS)):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_feeders(hass: HomeAssistant, cloud: FakeCloud) -> None:
    """Accounts without a supported feeder abort with a hint about the region."""
    cloud.responses["device_list"]["result"]["list"] = []
    with patch(f"{AUTH}.login_password", AsyncMock(return_value=CREDS)):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "u", "password": "p"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


async def test_reauth(hass: HomeAssistant, cloud: FakeCloud, entry: MockConfigEntry) -> None:
    """Reauth stores the new session and keeps the devices."""
    entry.add_to_hass(hass)
    new = XiaomiCredentials(OWNER, "c3M=", "new-token", "new-pass")
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch(f"{AUTH}.login_password", AsyncMock(return_value=new)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"login_method": "password"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "user@example.com", "password": "p"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_CREDENTIALS]["service_token"] == "new-token"
    assert entry.data[CONF_DEVICES][0]["did"] == DID


async def test_reauth_other_account(
    hass: HomeAssistant, cloud: FakeCloud, entry: MockConfigEntry
) -> None:
    """Logging in with another account is refused."""
    entry.add_to_hass(hass)
    other = XiaomiCredentials("42", "c3M=", "t", "p")
    result = await entry.start_reauth_flow(hass)
    with patch(f"{AUTH}.login_password", AsyncMock(return_value=other)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"login_method": "password"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "x", "password": "p"}
        )
    assert result["reason"] == "wrong_account"


async def test_reconfigure_devices(
    hass: HomeAssistant, cloud: FakeCloud, entry: MockConfigEntry
) -> None:
    """Feeders can be re-selected without logging in again."""
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "devices"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICES: [DID]}
    )
    assert result["reason"] == "reconfigure_successful"


async def test_options(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Polling intervals are configurable; real-time updates are on by default."""
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATUS_INTERVAL: 60, CONF_RECORDS_INTERVAL: 600}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_STATUS_INTERVAL: 60,
        CONF_RECORDS_INTERVAL: 600,
        CONF_OFFICIAL_PUSH: True,
    }
