"""Async client for the myjudo.eu cloud interface.

The i-soft SAFE+ has no local API, every read and write goes through
https://www.myjudo.eu/interface/. This module does not depend on Home
Assistant so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.myjudo.eu/interface/"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    )
}


class JudoError(Exception):
    """Base error."""


class JudoConnectionError(JudoError):
    """myjudo.eu could not be reached or answered with garbage."""


class JudoAuthError(JudoError):
    """Username or password were rejected."""


class JudoApiError(JudoError):
    """myjudo.eu answered with an error status."""


def int_to_le_hex(value: int, bits: int) -> str:
    """Encode an unsigned integer as little endian hex string.

    The old script silently produced wrong data for values that did not
    fit into the register (e.g. 70000 for a 16 bit register).
    """
    value = int(value)
    if bits not in (8, 16):
        raise ValueError(f"unsupported register size {bits}")
    if not 0 <= value < (1 << bits):
        raise ValueError(f"value {value} does not fit into {bits} bit")
    return value.to_bytes(bits // 8, "little").hex().upper()


@dataclass
class JudoDevice:
    """Raw data of one device as returned by "get device data"."""

    serial: str
    da: str
    dt: str
    registers: dict[str, str] = field(default_factory=dict)

    def register(self, index: int, start: int, end: int) -> int | None:
        """Read a little endian value from hex chars [start:end] of a register.

        Returns None if the register is missing or empty, so the caller
        never works with stale values (the old script kept the previous,
        already scaled value and scaled it a second time).
        """
        raw = self.registers.get(str(index))
        if not raw or len(raw) < end:
            return None
        try:
            return int.from_bytes(bytes.fromhex(raw[start:end]), "little")
        except ValueError:
            return None


def parse_devices(response: dict[str, Any]) -> dict[str, JudoDevice]:
    """Extract all devices from a "get device data" response."""
    devices: dict[str, JudoDevice] = {}
    for item in response.get("data") or []:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("serialnumber") or "")
        inner_list = item.get("data") or []
        if not serial or not inner_list or not isinstance(inner_list[0], dict):
            continue
        inner = inner_list[0]
        registers: dict[str, str] = {}
        for index, reg in (inner.get("data") or {}).items():
            if isinstance(reg, dict) and isinstance(reg.get("data"), str):
                registers[str(index)] = reg["data"]
        devices[serial] = JudoDevice(
            serial=serial,
            da=str(inner.get("da", "")),
            dt=str(inner.get("dt", "")),
            registers=registers,
        )
    return devices


class JudoApi:
    """Client for myjudo.eu."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        self._session = session
        self._username = username
        self._password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
        self._token: str | None = None
        self._login_lock = asyncio.Lock()

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        # Values are percent encoded (spaces as %20, like the web app does).
        # The old script inserted user name and password unencoded, which
        # broke logins with characters like "+" or "&".
        query = "&".join(
            f"{key}={quote(str(value), safe='')}" for key, value in params.items()
        )
        url = URL(f"{BASE_URL}?{query}", encoded=True)
        try:
            async with self._session.get(
                url, headers=HEADERS, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status >= 400:
                    # Do not chain the aiohttp error: its text contains the
                    # URL including the token.
                    raise JudoConnectionError(f"HTTP {resp.status}")
                text = await resp.text()
        except TimeoutError:
            raise JudoConnectionError("timeout") from None
        except aiohttp.ClientConnectorError as err:
            raise JudoConnectionError(f"cannot connect: {err.os_error}") from None
        except aiohttp.ClientError as err:
            raise JudoConnectionError(type(err).__name__) from None

        try:
            data = json.loads(text)
        except ValueError:
            raise JudoConnectionError("invalid response (no JSON)") from None
        if not isinstance(data, dict):
            raise JudoConnectionError("invalid response (no JSON object)")
        return data

    async def login(self) -> str:
        """Log in and return a fresh token."""
        data = await self._request(
            {
                "group": "register",
                "command": "login",
                "name": "login",
                "user": self._username,
                "password": self._password_md5,
                "nohash": "Service",
                "role": "customer",
            }
        )
        token = data.get("token")
        if not token:
            raise JudoAuthError("myjudo.eu login failed")
        self._token = str(token)
        _LOGGER.debug("Logged in to myjudo.eu")
        return self._token

    async def _ensure_token(self) -> str:
        async with self._login_lock:
            if self._token is None:
                await self.login()
            assert self._token is not None
            return self._token

    async def _authed_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Request with token, log in again once if the token expired."""
        for _ in range(2):
            token = await self._ensure_token()
            data = await self._request({"token": token, **params})
            if data.get("status") == "error" and data.get("data") == "login failed":
                _LOGGER.debug("Token expired, logging in again")
                if self._token == token:
                    self._token = None
                continue
            return data
        raise JudoApiError("token rejected after new login")

    async def get_devices(self) -> dict[str, JudoDevice]:
        """Return all devices of the account."""
        data = await self._authed_request(
            {"group": "register", "command": "get device data"}
        )
        if data.get("status") != "ok":
            raise JudoApiError(f"get device data: {data.get('data')}")
        return parse_devices(data)

    async def get_error_messages(self) -> list[dict[str, Any]]:
        """Return the fault memory, newest entry first."""
        data = await self._authed_request(
            {"group": "register", "command": "get error messages"}
        )
        if data.get("status") not in (None, "ok"):
            raise JudoApiError(f"get error messages: {data.get('data')}")
        entries = data.get("data")
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    async def write(self, device: JudoDevice, index: int, value: str = "") -> None:
        """Write a register of a device."""
        data = await self._authed_request(
            {
                "group": "register",
                "command": "write data",
                "serial_number": device.serial,
                "dt": device.dt,
                "index": index,
                "data": value,
                "da": device.da,
                "role": "customer",
            }
        )
        if data.get("status") != "ok":
            raise JudoApiError(f"write register {index}: {data.get('data')}")
