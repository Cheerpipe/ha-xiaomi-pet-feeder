"""Exceptions raised by the Xiaomi cloud client."""

from __future__ import annotations


class XiaomiError(Exception):
    """Base error for the Xiaomi cloud client."""


class XiaomiConnectionError(XiaomiError):
    """The cloud could not be reached or answered garbage."""


class XiaomiAuthError(XiaomiError):
    """Credentials or session are no longer valid."""


class XiaomiInvalidCredentials(XiaomiAuthError):
    """Wrong username or password."""


class XiaomiCaptchaRequired(XiaomiAuthError):
    """The account server asks for a captcha."""

    def __init__(self, image: bytes, wrong: bool = False) -> None:
        super().__init__("captcha required")
        self.image = image
        self.wrong = wrong


class XiaomiVerificationRequired(XiaomiAuthError):
    """Two-factor verification is needed; a code was sent to the user."""

    def __init__(self, method: str) -> None:
        super().__init__(f"verification required via {method}")
        self.method = method


class XiaomiVerificationFailed(XiaomiAuthError):
    """The two-factor code was rejected."""


class XiaomiBlocked(XiaomiAuthError):
    """The account server temporarily blocked the login (code 70036)."""


class XiaomiApiError(XiaomiError):
    """The gateway answered with an error code."""

    def __init__(self, path: str, code: int | None, message: str | None) -> None:
        super().__init__(f"{path}: code={code} message={message}")
        self.code = code
        self.message = message


class XiaomiDeviceError(XiaomiApiError):
    """The device rejected a property write or action."""
