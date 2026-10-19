"""HTTP-level tests of the Xiaomi account login."""

from __future__ import annotations

import asyncio
import json

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.xiaomi_pet_feeder.api import (
    XiaomiAuthenticator,
    XiaomiCaptchaRequired,
    XiaomiInvalidCredentials,
    XiaomiVerificationRequired,
)

ACCOUNT = "https://account.xiaomi.com"
STS = "https://sts.api.io.mi.com/sts?d=1"


def j(data: dict) -> str:
    """Account server answers are prefixed."""
    return "&&&START&&&" + json.dumps(data)


@pytest.fixture
async def auth(aioclient_mock: AiohttpClientMocker) -> XiaomiAuthenticator:
    """Authenticator on a mocked session."""
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield XiaomiAuthenticator(session, "CLIENTID12345678")
    await session.close()


def mock_sts(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(
        "https://sts.api.io.mi.com/sts",
        text="ok",
        cookies={"serviceToken": "ST", "userId": "42", "cUserId": "c42"},
    )


async def test_password_login(
    auth: XiaomiAuthenticator, aioclient_mock: AiohttpClientMocker
) -> None:
    """Sign, auth2 and STS produce a complete session."""
    aioclient_mock.get(f"{ACCOUNT}/pass/serviceLogin", text=j({"code": 70016, "_sign": "S"}))
    aioclient_mock.post(
        f"{ACCOUNT}/pass/serviceLoginAuth2",
        text=j({"code": 0, "ssecurity": "SS", "userId": 42, "passToken": "PT", "location": STS}),
    )
    mock_sts(aioclient_mock)
    creds = await auth.login_password("user@example.com", "secret")
    assert creds.as_dict() == {
        "user_id": "42",
        "ssecurity": "SS",
        "service_token": "ST",
        "pass_token": "PT",
        "c_user_id": "c42",
    }
    _, _, data, _ = aioclient_mock.mock_calls[1]
    assert data["hash"] == "5EBE2294ECD0E0F08EAB7690D2A6EE69"  # md5("secret"), upper case
    assert data["_sign"] == "S"


async def test_wrong_password(
    auth: XiaomiAuthenticator, aioclient_mock: AiohttpClientMocker
) -> None:
    """Code 70016 means wrong credentials."""
    aioclient_mock.get(f"{ACCOUNT}/pass/serviceLogin", text=j({"code": 70016, "_sign": "S"}))
    aioclient_mock.post(f"{ACCOUNT}/pass/serviceLoginAuth2", text=j({"code": 70016}))
    with pytest.raises(XiaomiInvalidCredentials):
        await auth.login_password("u", "p")


async def test_captcha(auth: XiaomiAuthenticator, aioclient_mock: AiohttpClientMocker) -> None:
    """The captcha image is returned, then the code is sent back."""
    aioclient_mock.get(f"{ACCOUNT}/pass/serviceLogin", text=j({"code": 70016, "_sign": "S"}))
    aioclient_mock.post(
        f"{ACCOUNT}/pass/serviceLoginAuth2", text=j({"code": 87001, "captchaUrl": "/pass/getCode"})
    )
    aioclient_mock.get(f"{ACCOUNT}/pass/getCode", content=b"JPEG")
    with pytest.raises(XiaomiCaptchaRequired) as err:
        await auth.login_password("u", "p")
    assert err.value.image == b"JPEG"

    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{ACCOUNT}/pass/serviceLoginAuth2",
        text=j({"code": 0, "ssecurity": "SS", "userId": 42, "location": STS}),
    )
    mock_sts(aioclient_mock)
    creds = await auth.submit_captcha("abcd")
    assert creds.service_token == "ST"
    assert aioclient_mock.mock_calls[0][2]["captCode"] == "abcd"


async def test_two_factor_email(
    auth: XiaomiAuthenticator, aioclient_mock: AiohttpClientMocker
) -> None:
    """E-mail is preferred, ssecurity comes from extension-pragma."""
    notification = f"{ACCOUNT}/fe/service/identity/authStart?sid=xiaomiio&context=CTX"
    aioclient_mock.get(f"{ACCOUNT}/pass/serviceLogin", text=j({"code": 70016, "_sign": "S"}))
    aioclient_mock.post(
        f"{ACCOUNT}/pass/serviceLoginAuth2", text=j({"code": 0, "notificationUrl": notification})
    )
    aioclient_mock.get(f"{ACCOUNT}/fe/service/identity/authStart", text="<html/>")
    aioclient_mock.get(f"{ACCOUNT}/identity/list", text=j({"flag": 4, "options": [4, 8]}))
    aioclient_mock.post(f"{ACCOUNT}/identity/auth/sendEmailTicket", text=j({"code": 0}))
    with pytest.raises(XiaomiVerificationRequired) as err:
        await auth.login_password("u", "p")
    assert err.value.method == "email"

    end = f"{ACCOUNT}/pass/serviceLoginAuth2/end?x=1"
    aioclient_mock.post(
        f"{ACCOUNT}/identity/auth/verifyEmail",
        text=j({"code": 0, "location": f"{ACCOUNT}/identity/result/check?x=1"}),
    )
    aioclient_mock.get(f"{ACCOUNT}/identity/result/check", status=302, headers={"Location": end})
    aioclient_mock.get(
        f"{ACCOUNT}/pass/serviceLoginAuth2/end",
        status=302,
        headers={"Location": STS, "extension-pragma": json.dumps({"ssecurity": "SS2"})},
    )
    mock_sts(aioclient_mock)
    creds = await auth.submit_code(" 123456 ")
    assert (creds.user_id, creds.ssecurity, creds.service_token) == ("42", "SS2", "ST")
    verify = next(c for c in aioclient_mock.mock_calls if "verifyEmail" in str(c[1]))
    assert verify[2]["ticket"] == "123456"
    assert verify[2]["_flag"] == "8"


async def test_refresh_with_pass_token(
    auth: XiaomiAuthenticator, aioclient_mock: AiohttpClientMocker
) -> None:
    """A valid passToken renews the session without any user input."""
    aioclient_mock.get(
        f"{ACCOUNT}/pass/serviceLogin",
        text=j({"code": 0, "ssecurity": "SS", "userId": 42, "passToken": "PT", "location": STS}),
    )
    mock_sts(aioclient_mock)
    creds = await auth.refresh("42", "PT")
    assert (creds.ssecurity, creds.service_token, creds.pass_token) == ("SS", "ST", "PT")


async def test_qr_login(auth: XiaomiAuthenticator, aioclient_mock: AiohttpClientMocker) -> None:
    """The QR image is downloaded and the long poll yields the session."""
    aioclient_mock.get(
        f"{ACCOUNT}/longPolling/loginUrl",
        text=j(
            {
                "code": 0,
                "qr": "https://qr.example/q.png",
                "loginUrl": "https://l",
                "lp": "https://lp.example/poll",
                "timeout": 300,
            }
        ),
    )
    aioclient_mock.get("https://qr.example/q.png", content=b"PNG")
    aioclient_mock.get(
        "https://lp.example/poll",
        text=j({"code": 0, "ssecurity": "SS", "userId": 42, "passToken": "PT", "location": STS}),
    )
    mock_sts(aioclient_mock)
    challenge = await auth.start_qr()
    assert challenge.image == b"PNG"
    creds = await auth.wait_qr(challenge)
    assert creds.service_token == "ST"
