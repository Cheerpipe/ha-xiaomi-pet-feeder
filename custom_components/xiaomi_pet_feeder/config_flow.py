"""Config flow: Xiaomi account login (password, captcha, 2FA or QR) and feeder selection."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Coroutine, Mapping
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    SERVERS,
    SUPPORTED_MODELS,
    QrChallenge,
    XiaomiAuthenticator,
    XiaomiAuthError,
    XiaomiBlocked,
    XiaomiCaptchaRequired,
    XiaomiCloudClient,
    XiaomiConnectionError,
    XiaomiCredentials,
    XiaomiError,
    XiaomiInvalidCredentials,
    XiaomiVerificationFailed,
    XiaomiVerificationRequired,
    new_client_id,
)
from .const import (
    CONF_CLIENT_ID,
    CONF_CREDENTIALS,
    CONF_DEVICES,
    CONF_LOGIN_METHOD,
    CONF_RECORDS_INTERVAL,
    CONF_SERVER,
    CONF_STATUS_INTERVAL,
    DEFAULT_RECORDS_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DOMAIN,
    SERVER_NAMES,
)
from .data import XiaomiPetFeederConfigEntry

_LOGGER = logging.getLogger(__name__)

METHOD_PASSWORD = "password"
METHOD_QR = "qr"


class XiaomiPetFeederConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup of a Xiaomi account."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._server = "de"
        self._client_id = new_client_id()
        self._username: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._auth: XiaomiAuthenticator | None = None
        self._credentials: XiaomiCredentials | None = None
        self._captcha: bytes | None = None
        self._verify_method = "email"
        self._qr: QrChallenge | None = None
        self._qr_task: asyncio.Task[XiaomiCredentials] | None = None
        self._qr_error: str | None = None
        self._devices: dict[str, dict[str, Any]] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: XiaomiPetFeederConfigEntry) -> OptionsFlowHandler:
        """Options flow."""
        return OptionsFlowHandler()

    # ----------------------------------------------------------- helpers
    def _authenticator(self) -> XiaomiAuthenticator:
        if self._auth is None:
            self._session = async_create_clientsession(
                self.hass, cookie_jar=aiohttp.CookieJar(), auto_cleanup=False
            )
            self._auth = XiaomiAuthenticator(self._session, self._client_id)
        return self._auth

    @callback
    def _close(self) -> None:
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
        if self._session is not None:
            self._session.detach()
            self._session = None
            self._auth = None

    @callback
    def async_remove(self) -> None:
        """Clean up when the flow is aborted by the user."""
        self._close()

    async def _login(
        self, coro: Coroutine[Any, Any, XiaomiCredentials]
    ) -> tuple[ConfigFlowResult | None, dict[str, str]]:
        """Run a login step: return the next flow result, or form errors."""
        try:
            self._credentials = await coro
        except XiaomiCaptchaRequired as err:
            self._captcha = err.image
            return await self.async_step_captcha(wrong=err.wrong), {}
        except XiaomiVerificationRequired as err:
            self._verify_method = err.method
            return await self.async_step_verify(), {}
        except XiaomiInvalidCredentials:
            return None, {"base": "invalid_auth"}
        except XiaomiVerificationFailed:
            return None, {"base": "invalid_code"}
        except XiaomiBlocked:
            return None, {"base": "blocked"}
        except XiaomiConnectionError:
            return None, {"base": "cannot_connect"}
        except XiaomiAuthError:
            _LOGGER.exception("Unexpected answer from the Xiaomi login")
            return None, {"base": "login_failed"}
        return await self._async_logged_in(), {}

    async def _async_logged_in(self) -> ConfigFlowResult:
        assert self._credentials is not None
        self._close()
        await self.async_set_unique_id(f"{self._credentials.user_id}_{self._server}")
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_CREDENTIALS: self._credentials.as_dict(),
                    CONF_CLIENT_ID: self._client_id,
                    CONF_USERNAME: self._username or entry.data.get(CONF_USERNAME),
                },
            )
        self._abort_if_unique_id_configured()
        return await self.async_step_devices()

    # ----------------------------------------------------------- steps
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose region and login method."""
        if user_input is not None:
            self._server = user_input[CONF_SERVER]
            if user_input[CONF_LOGIN_METHOD] == METHOD_QR:
                return await self.async_step_qr()
            return await self.async_step_password()
        return self.async_show_form(step_id="user", data_schema=self._method_schema())

    def _method_schema(self, include_server: bool = True) -> vol.Schema:
        schema: dict[Any, Any] = {}
        if include_server:
            schema[vol.Required(CONF_SERVER, default=self._server)] = SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=s, label=SERVER_NAMES[s]) for s in SERVERS],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_SERVER,
                )
            )
        schema[vol.Required(CONF_LOGIN_METHOD, default=METHOD_PASSWORD)] = SelectSelector(
            SelectSelectorConfig(
                options=[METHOD_PASSWORD, METHOD_QR],
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_LOGIN_METHOD,
            )
        )
        return vol.Schema(schema)

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Username and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            result, errors = await self._login(
                self._authenticator().login_password(self._username, user_input[CONF_PASSWORD])
            )
            if result is not None:
                return result
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self._username or ""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD, autocomplete="current-password"
                    )
                ),
            }
        )
        return self.async_show_form(step_id="password", data_schema=schema, errors=errors)

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None, wrong: bool = False
    ) -> ConfigFlowResult:
        """Show the captcha image."""
        errors: dict[str, str] = {"base": "invalid_captcha"} if wrong else {}
        if user_input is not None:
            result, errors = await self._login(
                self._authenticator().submit_captcha(user_input["captcha"])
            )
            if result is not None:
                return result
        return self.async_show_form(
            step_id="captcha",
            data_schema=vol.Schema({vol.Required("captcha"): str}),
            description_placeholders={
                "captcha_image": base64.b64encode(self._captcha or b"").decode()
            },
            errors=errors,
        )

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the code sent by e-mail or SMS."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result, errors = await self._login(
                self._authenticator().submit_code(user_input["code"])
            )
            if result is not None:
                return result
        return self.async_show_form(
            step_id="verify",
            data_schema=vol.Schema({vol.Required("code"): str}),
            description_placeholders={"method": self._verify_method},
            errors=errors,
        )

    async def async_step_qr(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show a QR code and wait until it is confirmed in the Xiaomi Home app."""
        if self._qr_task is None:
            try:
                self._qr = await self._authenticator().start_qr()
            except XiaomiError:
                self._close()
                return self.async_abort(reason="cannot_connect")
            self._qr_task = self.hass.async_create_task(
                self._authenticator().wait_qr(self._qr), eager_start=False
            )
        if not self._qr_task.done():
            assert self._qr is not None
            return self.async_show_progress(
                step_id="qr",
                progress_action="scan_qr",
                progress_task=self._qr_task,
                description_placeholders={
                    "qr_image": base64.b64encode(self._qr.image).decode(),
                    "login_url": self._qr.login_url,
                },
            )
        try:
            self._credentials = self._qr_task.result()
        except asyncio.CancelledError:
            self._qr_error = "qr_expired"
        except XiaomiError:
            self._qr_error = "qr_expired"
        self._qr_task = None
        if self._qr_error:
            return self.async_show_progress_done(next_step_id="qr_failed")
        return self.async_show_progress_done(next_step_id="qr_done")

    async def async_step_qr_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """QR confirmed."""
        return await self._async_logged_in()

    async def async_step_qr_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """QR expired or rejected."""
        self._close()
        return self.async_abort(reason=self._qr_error or "qr_expired")

    async def _load_devices(self) -> list[dict[str, Any]]:
        assert self._credentials is not None
        client = XiaomiCloudClient(
            async_get_clientsession(self.hass), self._server, self._client_id, self._credentials
        )
        devices = [d for d in await client.device_list() if d.get("model") in SUPPORTED_MODELS]
        homes = await client.homes()
        result = []
        for dev in devices:
            did = str(dev["did"])
            home_id = home_owner = None
            for home in homes:
                dids = {str(x) for x in home.get("dids") or []}
                for room in home.get("roomlist") or []:
                    dids |= {str(x) for x in room.get("dids") or []}
                if did in dids:
                    home_id, home_owner = home.get("id"), home.get("uid")
            result.append(
                {
                    "did": did,
                    "name": dev.get("name") or "Pet Feeder",
                    "model": dev["model"],
                    "owner_id": str(dev.get("uid") or self._credentials.user_id),
                    "home_id": home_id,
                    "home_owner": home_owner,
                }
            )
        return result

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which feeders to add."""
        if user_input is not None:
            chosen = [self._devices[did] for did in user_input[CONF_DEVICES]]
            assert self._credentials is not None
            data = {
                CONF_SERVER: self._server,
                CONF_CLIENT_ID: self._client_id,
                CONF_USERNAME: self._username,
                CONF_CREDENTIALS: self._credentials.as_dict(),
                CONF_DEVICES: chosen,
            }
            if self.source == SOURCE_RECONFIGURE:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(), data_updates={CONF_DEVICES: chosen}
                )
            title = f"{self._username or self._credentials.user_id} ({self._server})"
            return self.async_create_entry(title=title, data=data)

        try:
            devices = await self._load_devices()
        except XiaomiAuthError:
            return self.async_abort(reason="invalid_auth")
        except XiaomiError:
            return self.async_abort(reason="cannot_connect")
        if not devices:
            return self.async_abort(
                reason="no_devices", description_placeholders={"server": self._server}
            )
        self._devices = {d["did"]: d for d in devices}
        options = [
            SelectOptionDict(value=d["did"], label=f"{d['name']} ({d['model']})") for d in devices
        ]
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICES, default=list(self._devices)): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=True)
                    )
                }
            ),
        )

    # ----------------------------------------------------------- reauth
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication when the stored session cannot be renewed."""
        self._server = entry_data[CONF_SERVER]
        self._client_id = entry_data[CONF_CLIENT_ID]
        self._username = entry_data.get(CONF_USERNAME)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick how to log in again."""
        if user_input is not None:
            if user_input[CONF_LOGIN_METHOD] == METHOD_QR:
                return await self.async_step_qr()
            return await self.async_step_password()
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=self._method_schema(include_server=False)
        )

    # ------------------------------------------------------- reconfigure
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change which feeders are included."""
        entry = self._get_reconfigure_entry()
        self._server = entry.data[CONF_SERVER]
        self._client_id = entry.data[CONF_CLIENT_ID]
        self._username = entry.data.get(CONF_USERNAME)
        if self._credentials is None:
            if entry.state is ConfigEntryState.LOADED:
                self._credentials = entry.runtime_data.client.credentials
            else:
                self._credentials = XiaomiCredentials.from_dict(entry.data[CONF_CREDENTIALS])
        return await self.async_step_devices(user_input)


class OptionsFlowHandler(OptionsFlowWithReload):
    """Polling intervals."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data={k: int(v) for k, v in user_input.items()})
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_STATUS_INTERVAL,
                        default=options.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=10,
                            max=600,
                            step=5,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_RECORDS_INTERVAL,
                        default=options.get(CONF_RECORDS_INTERVAL, DEFAULT_RECORDS_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=60,
                            max=3600,
                            step=30,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
