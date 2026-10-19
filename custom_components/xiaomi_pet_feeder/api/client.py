"""Signed client for the Mi Home cloud gateway (``https://<region>.api.io.mi.com/app``)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import time
from typing import Any

import aiohttp

from .auth import XiaomiCredentials, user_agent
from .crypto import build_rc4_form, decrypt
from .exceptions import XiaomiApiError, XiaomiAuthError, XiaomiConnectionError, XiaomiDeviceError

_LOGGER = logging.getLogger(__name__)

SERVERS = ("cn", "de", "i2", "ru", "sg", "us", "tw")
TIMEOUT = aiohttp.ClientTimeout(total=25)
_TOKEN_EXPIRED_MESSAGES = ("auth err", "invalid signature", "servicetoken_expired")

RefreshCallback = Callable[[], Awaitable[XiaomiCredentials]]


def api_base(server: str) -> str:
    """Return the base URL of the gateway for a region."""
    return f"https://{'' if server == 'cn' else server + '.'}api.io.mi.com/app"


def _tz_cookie() -> str:
    offset = -(time.altzone if time.localtime().tm_isdst else time.timezone)
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return f"GMT{sign}{offset // 3600:02d}:{(offset % 3600) // 60:02d}"


class XiaomiCloudClient:
    """Low level signed requests, with transparent token renewal."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        server: str,
        client_id: str,
        credentials: XiaomiCredentials,
        refresh: RefreshCallback | None = None,
    ) -> None:
        """Create a client; ``refresh`` renews credentials on token expiry."""
        self._session = session
        self.server = server
        self._base = api_base(server)
        self._ua = user_agent(client_id)
        self.credentials = credentials
        self._refresh = refresh
        self._refresh_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        creds = self.credentials
        cookies = {
            "userId": creds.user_id,
            "yetAnotherServiceToken": creds.service_token,
            "serviceToken": creds.service_token,
            "locale": "en_US",
            "timezone": _tz_cookie(),
            "is_daylight": str(time.daylight),
            "dst_offset": str(time.localtime().tm_isdst * 3_600_000),
            "channel": "MI_APP_STORE",
        }
        return {
            "User-Agent": self._ua,
            "X-XIAOMI-PROTOCAL-FLAG-CLI": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        }

    async def _post(self, path: str, data: Any) -> tuple[int, Any]:
        payload = json.dumps(data, separators=(",", ":"))
        form, key = build_rc4_form("/app" + path, payload, self.credentials.ssecurity)
        try:
            async with self._session.post(
                self._base + path, data=form, headers=self._headers(), timeout=TIMEOUT
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise XiaomiConnectionError(f"{path}: {err}") from err

        if status in (401, 403):
            return status, {"code": 3, "message": "auth err"}
        stripped = text.lstrip()
        if not stripped:
            raise XiaomiConnectionError(f"{path}: empty response (HTTP {status})")
        if stripped.startswith("{"):
            body = json.loads(stripped)
        else:
            try:
                body = json.loads(decrypt(key, stripped))
            except ValueError as err:
                raise XiaomiConnectionError(f"{path}: undecodable response") from err
        return status, body

    @staticmethod
    def _expired(body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        message = str(body.get("message") or "").lower()
        return body.get("code") in (2, 3) or any(m in message for m in _TOKEN_EXPIRED_MESSAGES)

    async def request(self, path: str, data: Any) -> Any:
        """POST ``data`` to ``path`` and return the ``result`` field."""
        _, body = await self._post(path, data)
        if self._expired(body):
            await self._renew()
            _, body = await self._post(path, data)
            if self._expired(body):
                raise XiaomiAuthError(f"{path}: session rejected after renewal")
        if not isinstance(body, dict):
            raise XiaomiConnectionError(f"{path}: unexpected response")
        if body.get("code", 0) != 0:
            raise XiaomiApiError(path, body.get("code"), body.get("message"))
        return body.get("result")

    async def _renew(self) -> None:
        if self._refresh is None:
            raise XiaomiAuthError("session expired")
        old = self.credentials.service_token
        async with self._refresh_lock:
            if self.credentials.service_token != old:
                return  # another request already renewed it
            _LOGGER.debug("Xiaomi session expired, renewing with passToken")
            self.credentials = await self._refresh()

    # ----------------------------------------------------------- helpers
    async def device_list(self) -> list[dict[str, Any]]:
        """All devices of the account in this region."""
        result = await self.request(
            "/home/device_list",
            {
                "getVirtualModel": True,
                "getHuamiDevices": 1,
                "get_split_device": False,
                "support_smart_home": True,
            },
        )
        return list((result or {}).get("list") or [])

    async def homes(self) -> list[dict[str, Any]]:
        """Homes of the account (needed by the consumables API)."""
        result = await self.request(
            "/v2/homeroom/gethome",
            {
                "fg": True,
                "fetch_share": True,
                "fetch_share_dev": True,
                "limit": 300,
                "app_ver": 7,
            },
        )
        return list((result or {}).get("homelist") or [])

    async def get_properties(
        self, did: str, props: list[tuple[int, int]]
    ) -> dict[tuple[int, int], Any]:
        """Read MIoT properties; failed ones are left out."""
        result = await self.request(
            "/miotspec/prop/get",
            {"params": [{"did": did, "siid": s, "piid": p} for s, p in props]},
        )
        values: dict[tuple[int, int], Any] = {}
        for item in result or []:
            if item.get("code", 0) == 0 and "value" in item:
                values[(item["siid"], item["piid"])] = item["value"]
        return values

    async def set_property(self, did: str, siid: int, piid: int, value: Any) -> None:
        """Write one MIoT property."""
        result = await self.request(
            "/miotspec/prop/set",
            {"params": [{"did": did, "siid": siid, "piid": piid, "value": value}]},
        )
        for item in result or []:
            if item.get("code", 0) not in (0, 1):
                raise XiaomiDeviceError("/miotspec/prop/set", item.get("code"), None)

    async def action(self, did: str, siid: int, aiid: int, ins: list[Any]) -> Any:
        """Run a MIoT action with positional input values."""
        result = await self.request(
            "/miotspec/action",
            {"params": {"did": did, "siid": siid, "aiid": aiid, "in": ins}},
        )
        if isinstance(result, dict) and result.get("code", 0) not in (0, 1):
            raise XiaomiDeviceError("/miotspec/action", result.get("code"), None)
        return result
