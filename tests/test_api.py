"""Tests for the myjudo.eu client."""

from __future__ import annotations

import json
from typing import Any

import pytest

from custom_components.judo_isoft_save_plus.api import (
    JudoApi,
    JudoAuthError,
    JudoConnectionError,
    int_to_le_hex,
    parse_devices,
)

from .conftest import SERIAL, build_response


def test_int_to_le_hex() -> None:
    assert int_to_le_hex(8, 8) == "08"
    assert int_to_le_hex(2500, 16) == "C409"
    assert int_to_le_hex(25000, 16) == "A861"
    with pytest.raises(ValueError):
        int_to_le_hex(70000, 16)
    with pytest.raises(ValueError):
        int_to_le_hex(-1, 8)


def test_parse_devices() -> None:
    devices = parse_devices(build_response())
    device = devices[SERIAL]
    assert device.da == "da1"
    assert device.dt == "dt1"
    assert device.register(8, 0, 8) == 123456
    assert device.register(790, 54, 56) == 20
    # missing/short register -> None instead of a stale value
    assert device.register(999, 0, 4) is None
    assert device.register(7, 0, 40) is None


def test_parse_devices_garbage() -> None:
    assert parse_devices({"data": None}) == {}
    assert parse_devices({"data": [None, {"serialnumber": "1"}]}) == {}


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url, **kwargs) -> FakeResponse:
        self.urls.append(str(url))
        item = self.responses.pop(0)
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(item)


async def test_login_encodes_credentials() -> None:
    session = FakeSession([{"token": "abc"}])
    api = JudoApi(session, "a+b@example.com", "p&w")
    assert await api.login() == "abc"
    url = session.urls[0]
    assert "user=a%2Bb%40example.com" in url
    assert "p&w" not in url


async def test_login_failed() -> None:
    api = JudoApi(FakeSession([{"status": "error"}]), "u", "p")
    with pytest.raises(JudoAuthError):
        await api.login()


async def test_relogin_on_expired_token() -> None:
    session = FakeSession(
        [
            {"token": "t1"},
            {"status": "error", "data": "login failed"},
            {"token": "t2"},
            build_response(),
        ]
    )
    api = JudoApi(session, "u", "p")
    devices = await api.get_devices()
    assert SERIAL in devices
    assert "token=t2" in session.urls[-1]
    assert "command=get%20device%20data" in session.urls[-1]


async def test_http_error_hides_token() -> None:
    session = FakeSession([{"token": "secret-token"}, FakeResponse("", 500)])
    api = JudoApi(session, "u", "p")
    with pytest.raises(JudoConnectionError) as err:
        await api.get_devices()
    assert "secret-token" not in str(err.value)


async def test_invalid_json() -> None:
    api = JudoApi(FakeSession(["<html>"]), "u", "p")
    with pytest.raises(JudoConnectionError):
        await api.login()


async def test_write_params() -> None:
    session = FakeSession([{"token": "t"}, {"status": "ok"}])
    api = JudoApi(session, "u", "p")
    device = parse_devices(build_response())[SERIAL]
    await api.write(device, 60, "08")
    url = session.urls[-1]
    for part in (
        "command=write%20data",
        f"serial_number={SERIAL}",
        "dt=dt1",
        "da=da1",
        "index=60",
        "data=08",
        "role=customer",
    ):
        assert part in url
