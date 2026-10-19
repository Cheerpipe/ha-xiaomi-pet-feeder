"""Login against account.xiaomi.com (password, captcha, 2FA, QR and passToken).

The flow mirrors what the Mi Home app does for the ``xiaomiio`` service:

1. ``GET /pass/serviceLogin`` returns a ``_sign`` (or a full session when a valid
   ``passToken`` cookie is present).
2. ``POST /pass/serviceLoginAuth2`` with the MD5 of the password. The answer is
   either a session, a captcha challenge or a two-factor ``notificationUrl``.
3. ``GET location`` on ``sts.api.io.mi.com`` hands out the ``serviceToken``.

Every request goes through a dedicated aiohttp session so the cookie jar of
the identity flow is isolated from the rest of Home Assistant.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import logging
import re
import secrets
import string
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
from yarl import URL

from .exceptions import (
    XiaomiAuthError,
    XiaomiBlocked,
    XiaomiCaptchaRequired,
    XiaomiConnectionError,
    XiaomiInvalidCredentials,
    XiaomiVerificationFailed,
    XiaomiVerificationRequired,
)

_LOGGER = logging.getLogger(__name__)

ACCOUNT_URL = "https://account.xiaomi.com"
SID = "xiaomiio"
STS_CALLBACK = "https://sts.api.io.mi.com/sts"
QS = "%3Fsid%3Dxiaomiio%26_json%3Dtrue"
TIMEOUT = aiohttp.ClientTimeout(total=30)

VERIFY_PHONE = 4
VERIFY_EMAIL = 8
_VERIFY_NAMES = {VERIFY_PHONE: "phone", VERIFY_EMAIL: "email"}


def new_client_id() -> str:
    """Random client identifier, kept for the life of the config entry."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def user_agent(client_id: str) -> str:
    """User agent of the Android Mi Home app, as expected by the account server."""
    return f"Android-7.1.1-1.0.0-ONEPLUS A3010-136-{client_id} APP/xiaomi.smarthome APPV/62830"


@dataclass(slots=True)
class XiaomiCredentials:
    """Everything needed to sign cloud requests and to renew them silently."""

    user_id: str
    ssecurity: str
    service_token: str
    pass_token: str | None = None
    c_user_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize for storage in the config entry."""
        return {
            "user_id": self.user_id,
            "ssecurity": self.ssecurity,
            "service_token": self.service_token,
            "pass_token": self.pass_token,
            "c_user_id": self.c_user_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> XiaomiCredentials:
        """Load from config entry data."""
        return cls(
            user_id=str(data["user_id"]),
            ssecurity=data["ssecurity"],
            service_token=data["service_token"],
            pass_token=data.get("pass_token"),
            c_user_id=data.get("c_user_id"),
        )


@dataclass(slots=True)
class QrChallenge:
    """A QR code login waiting to be scanned."""

    image: bytes
    login_url: str
    poll_url: str
    expires_at: float


@dataclass(slots=True)
class _State:
    """Mutable state of an ongoing interactive login."""

    username: str | None = None
    post: dict[str, str] = field(default_factory=dict)
    captcha_for: str | None = None  # "login" | "ticket"
    context: str | None = None
    verify_flag: int | None = None
    user_id: str | None = None
    c_user_id: str | None = None
    ssecurity: str | None = None
    pass_token: str | None = None
    service_token: str | None = None


def _decode(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text.replace("&&&START&&&", "", 1))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class XiaomiAuthenticator:
    """Interactive login helper used by the config flow and for token renewal."""

    def __init__(self, session: aiohttp.ClientSession, client_id: str) -> None:
        """Use a session with its own cookie jar; it is not closed here."""
        self._session = session
        self.client_id = client_id
        self._ua = user_agent(client_id)
        self._state = _State()
        account = URL(ACCOUNT_URL)
        session.cookie_jar.update_cookies({"sdkVersion": "3.8.6", "deviceId": client_id}, account)

    # ------------------------------------------------------------------ http
    async def _request(
        self,
        method: str,
        url: str,
        *,
        allow_redirects: bool = True,
        **kwargs: Any,
    ) -> tuple[aiohttp.ClientResponse, str]:
        if not url.startswith("http"):
            url = ACCOUNT_URL + url
        headers = {"User-Agent": self._ua}
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                allow_redirects=allow_redirects,
                timeout=TIMEOUT,
                **kwargs,
            ) as resp:
                body = await resp.read()
                self._harvest(resp)
                for hop in resp.history:
                    self._harvest(hop)
                return resp, body.decode(errors="replace")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise XiaomiConnectionError(f"{method} {urlparse(url).path}: {err}") from err

    async def _read_bytes(self, url: str) -> bytes:
        if not url.startswith("http"):
            url = ACCOUNT_URL + url
        try:
            async with self._session.get(
                url, headers={"User-Agent": self._ua}, timeout=TIMEOUT
            ) as resp:
                self._harvest(resp)
                return await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise XiaomiConnectionError(f"GET image: {err}") from err

    def _harvest(self, resp: aiohttp.ClientResponse) -> None:
        """Collect identity values from cookies and headers of any response."""
        st = self._state
        for name, morsel in resp.cookies.items():
            if not morsel.value:
                continue
            if name == "serviceToken":
                st.service_token = morsel.value
            elif name == "userId":
                st.user_id = morsel.value
            elif name == "cUserId":
                st.c_user_id = morsel.value
            elif name == "passToken":
                st.pass_token = morsel.value
        if pragma := resp.headers.get("extension-pragma"):
            try:
                if ssecurity := json.loads(pragma).get("ssecurity"):
                    st.ssecurity = ssecurity
            except ValueError:
                pass

    def _credentials(self) -> XiaomiCredentials:
        st = self._state
        if not (st.user_id and st.ssecurity and st.service_token):
            raise XiaomiAuthError("login finished without a complete session")
        return XiaomiCredentials(
            user_id=str(st.user_id),
            ssecurity=st.ssecurity,
            service_token=st.service_token,
            pass_token=st.pass_token,
            c_user_id=st.c_user_id,
        )

    def _take_session(self, data: dict[str, Any]) -> None:
        st = self._state
        st.user_id = str(data.get("userId") or st.user_id or "") or None
        st.c_user_id = data.get("cUserId") or st.c_user_id
        st.ssecurity = data.get("ssecurity") or st.ssecurity
        st.pass_token = data.get("passToken") or st.pass_token

    # ------------------------------------------------------------- steps
    async def _service_login(self, cookies: dict[str, str] | None = None) -> dict[str, Any]:
        if cookies:
            self._session.cookie_jar.update_cookies(cookies, URL(ACCOUNT_URL))
        _, text = await self._request(
            "GET", "/pass/serviceLogin", params={"sid": SID, "_json": "true"}
        )
        data = _decode(text)
        if data.get("code") == 0 and data.get("ssecurity"):
            self._take_session(data)
        return data

    async def _sts(self, location: str) -> XiaomiCredentials:
        self._state.service_token = None
        await self._request("GET", location)
        return self._credentials()

    async def login_password(self, username: str, password: str) -> XiaomiCredentials:
        """Start a password login.

        Raises XiaomiCaptchaRequired or XiaomiVerificationRequired when the user
        has to provide more input; continue with submit_captcha / submit_code.
        """
        self._state = _State(username=username)
        sign = await self._service_login({"userId": username})
        if sign.get("code") == 0 and sign.get("location"):
            return await self._sts(sign["location"])
        self._state.post = {
            "user": username,
            "hash": hashlib.md5(password.encode()).hexdigest().upper(),
            "callback": sign.get("callback") or STS_CALLBACK,
            "sid": sign.get("sid") or SID,
            "qs": sign.get("qs") or QS,
            "_sign": sign.get("_sign") or "",
        }
        return await self._auth2()

    async def _auth2(self, captcha: str | None = None) -> XiaomiCredentials:
        post = dict(self._state.post)
        params = {"_json": "true"}
        if captcha:
            post["captCode"] = captcha
            params["_dc"] = str(int(time.time() * 1000))
        _, text = await self._request("POST", "/pass/serviceLoginAuth2", data=post, params=params)
        auth = _decode(text)
        code = auth.get("code")
        if auth.get("location") and auth.get("ssecurity"):
            self._take_session(auth)
            return await self._sts(auth["location"])
        if notification := auth.get("notificationUrl"):
            await self._start_verification(notification)
        if captcha_url := auth.get("captchaUrl"):
            self._state.captcha_for = "login"
            image = await self._read_bytes(captcha_url)
            raise XiaomiCaptchaRequired(image, wrong=code == 87001)
        if code in (20003, 70002, 70016):
            raise XiaomiInvalidCredentials(f"credentials rejected (code {code})")
        if code == 70036:
            raise XiaomiBlocked("login temporarily blocked for this IP (code 70036)")
        raise XiaomiAuthError(f"serviceLoginAuth2 failed (code {code})")

    async def submit_captcha(self, code: str) -> XiaomiCredentials:
        """Continue after the user typed the captcha text."""
        if self._state.captcha_for == "ticket":
            await self._send_ticket(icode=code)
            raise XiaomiVerificationRequired(_VERIFY_NAMES[self._state.verify_flag or 4])
        return await self._auth2(captcha=code)

    async def _start_verification(self, notification_url: str) -> None:
        if not notification_url.startswith("http"):
            notification_url = ACCOUNT_URL + notification_url
        query = parse_qs(urlparse(notification_url).query)
        self._state.context = (query.get("context") or [""])[0]
        await self._request("GET", notification_url)
        _, text = await self._request(
            "GET",
            "/identity/list",
            params={"sid": SID, "context": self._state.context, "_locale": "en_US"},
        )
        info = _decode(text)
        options = [o for o in info.get("options") or [info.get("flag")] if o in _VERIFY_NAMES]
        # Email first: SMS is often rate limited for foreign numbers.
        self._state.verify_flag = VERIFY_EMAIL if VERIFY_EMAIL in options else VERIFY_PHONE
        await self._send_ticket()
        raise XiaomiVerificationRequired(_VERIFY_NAMES[self._state.verify_flag])

    async def _send_ticket(self, icode: str = "") -> None:
        flag = self._state.verify_flag
        api = (
            "/identity/auth/sendEmailTicket"
            if flag == VERIFY_EMAIL
            else "/identity/auth/sendPhoneTicket"
        )
        ick = self._cookie("ick")
        _, text = await self._request(
            "POST",
            api,
            params={
                "_dc": str(int(time.time() * 1000)),
                "sid": SID,
                "context": self._state.context or "",
                "mask": "0",
                "_locale": "en_US",
            },
            data={"retry": "0", "icode": icode, "_json": "true", "ick": ick},
        )
        sent = _decode(text)
        if sent.get("code") not in (0, None) and (captcha_url := sent.get("captchaUrl")):
            self._state.captcha_for = "ticket"
            raise XiaomiCaptchaRequired(await self._read_bytes(captcha_url))
        if sent.get("code") not in (0, None):
            _LOGGER.debug("Sending the verification code returned %s", sent.get("code"))

    def _cookie(self, name: str) -> str:
        for cookie in self._session.cookie_jar:
            if cookie.key == name:
                return cookie.value
        return ""

    async def submit_code(self, ticket: str) -> XiaomiCredentials:
        """Finish a two-factor login with the code the user received."""
        flag = self._state.verify_flag or VERIFY_PHONE
        api = "/identity/auth/verifyEmail" if flag == VERIFY_EMAIL else "/identity/auth/verifyPhone"
        resp, text = await self._request(
            "POST",
            api,
            params={
                "_flag": str(flag),
                "_json": "true",
                "sid": SID,
                "context": self._state.context or "",
                "mask": "0",
                "_locale": "en_US",
            },
            data={
                "_flag": str(flag),
                "ticket": ticket.strip(),
                "trust": "false",
                "_json": "true",
                "ick": self._cookie("ick"),
            },
        )
        result = _decode(text)
        location = result.get("location") or resp.headers.get("Location")
        if not location and (
            match := re.search(r"https://account\.xiaomi\.com/identity/result/check\?[^\"']+", text)
        ):
            location = match.group(0)
        if not location:
            raise XiaomiVerificationFailed(f"code rejected ({result.get('code')})")

        self._state.ssecurity = None
        self._state.service_token = None
        await self._walk(location)
        if self._state.ssecurity and self._state.service_token:
            return self._credentials()
        # Second strategy: the verified session can now log in without password.
        data = await self._service_login()
        if data.get("code") == 0 and data.get("location"):
            return await self._sts(data["location"])
        raise XiaomiAuthError("verification accepted but no session was issued")

    async def _walk(self, url: str) -> None:
        """Follow the post-verification redirect chain by hand."""
        retried_tips = False
        for _ in range(15):
            if not url.startswith("http"):
                url = ACCOUNT_URL + url
            resp, text = await self._request("GET", url, allow_redirects=False)
            nxt = resp.headers.get("Location")
            if (
                resp.status == 200
                and not nxt
                and "serviceLoginAuth2/end" in url
                and not retried_tips
            ):
                retried_tips = True
                continue
            if not nxt and resp.status == 200 and "sts.api.io.mi.com" not in url:
                if match := re.search(r"https://sts\.api\.io\.mi\.com/sts[^\"'<>\s]*", text):
                    nxt = match.group(0).replace("&amp;", "&")
                else:
                    parsed = urlparse(str(resp.url))
                    skip = (parse_qs(parsed.query).get("skipUrl") or [None])[0]
                    if skip and parsed.path.startswith("/fe/"):
                        nxt = skip
            if not nxt:
                return
            url = nxt

    # ------------------------------------------------------------- QR login
    async def start_qr(self) -> QrChallenge:
        """Request a QR code that the user scans with the Xiaomi Home app."""
        self._state = _State()
        _, text = await self._request(
            "GET",
            "/longPolling/loginUrl",
            params={
                "_qrsize": "480",
                "qs": QS,
                "callback": STS_CALLBACK,
                "_hasLogo": "false",
                "sid": SID,
                "serviceParam": "",
                "_locale": "en_US",
                "_dc": str(int(time.time() * 1000)),
            },
        )
        data = _decode(text)
        if not data.get("lp") or not data.get("qr"):
            raise XiaomiAuthError(f"QR login unavailable (code {data.get('code')})")
        image = await self._read_bytes(data["qr"])
        return QrChallenge(
            image=image,
            login_url=data.get("loginUrl", ""),
            poll_url=data["lp"],
            expires_at=time.monotonic() + int(data.get("timeout") or 300),
        )

    async def wait_qr(self, challenge: QrChallenge) -> XiaomiCredentials:
        """Long-poll until the QR code is confirmed in the app."""
        while time.monotonic() < challenge.expires_at:
            try:
                async with self._session.get(
                    challenge.poll_url,
                    headers={"User-Agent": self._ua},
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as resp:
                    text = await resp.text()
                    status = resp.status
            except TimeoutError:
                continue
            except aiohttp.ClientError:
                await asyncio.sleep(2)
                continue
            if status == 200:
                data = _decode(text)
                if data.get("ssecurity") and data.get("location"):
                    self._take_session(data)
                    return await self._sts(data["location"])
            await asyncio.sleep(1)
        raise XiaomiAuthError("QR code expired")

    # ------------------------------------------------------------- renewal
    async def refresh(self, user_id: str, pass_token: str) -> XiaomiCredentials:
        """Get a fresh serviceToken with the long-lived passToken (no user input)."""
        self._state = _State(pass_token=pass_token, user_id=user_id)
        data = await self._service_login({"passToken": pass_token, "userId": str(user_id)})
        if data.get("code") == 0 and data.get("location"):
            return await self._sts(data["location"])
        raise XiaomiAuthError(f"passToken rejected (code {data.get('code')})")
