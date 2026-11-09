"""Fixtures for the JUDO i-soft SAFE+ tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.judo_isoft_save_plus.api import JudoDevice, parse_devices

SERIAL = "123456"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    yield


def le(value: int, nbytes: int) -> str:
    return value.to_bytes(nbytes, "little").hex()


def build_response(
    *,
    total_l: int = 123456,
    soft_l: int = 100000,
    regenerations: int = 50,
    regen_running: bool = False,
    water_lock: bool = False,
    holiday_raw: int = 0,
) -> dict[str, Any]:
    """Build a "get device data" response with realistic registers."""
    reg790 = ["00"] * 30
    reg790[9] = le(8, 1)  # output hardness  [18:20]
    reg790[17:19] = [le(1200, 2)[:2], le(1200, 2)[2:]]  # water flow [34:38]
    reg790[27] = le(20, 1)  # input hardness [54:56]
    reg791 = ["00"] * 34
    reg791[1] = "01" if regen_running else "00"  # [2:4]
    reg791[31:33] = [le(regenerations, 2)[:2], le(regenerations, 2)[2:]]  # [62:66]
    reg792 = ["00"] * 20
    reg792[1] = "01" if water_lock else "00"  # [2:4]
    reg792[10] = le(2, 1)  # sleepmode [20:22]
    reg792[13:15] = [le(2500, 2)[:2], le(2500, 2)[2:]]  # max waterflow [26:30]
    reg792[15:17] = [le(300, 2)[:2], le(300, 2)[2:]]  # extraction quantity [30:34]
    reg792[17:19] = [le(30, 2)[:2], le(30, 2)[2:]]  # extraction time [34:38]
    reg792[19] = le(holiday_raw, 1)  # holiday [38:40]
    registers = {
        "7": {"data": le(24 * 100, 2)},
        "8": {"data": le(total_l, 4)},
        "9": {"data": le(soft_l, 4)},
        "93": {"data": "000000" + le(87, 1)},
        "94": {"data": le(25000, 2) + le(40, 2)},
        "790": {"data": "".join(reg790)},
        "791": {"data": "".join(reg791)},
        "792": {"data": "".join(reg792)},
    }
    return {
        "status": "ok",
        "data": [
            {
                "serialnumber": SERIAL,
                "data": [{"da": "da1", "dt": "dt1", "data": registers}],
            }
        ],
    }


def devices(**kwargs: Any) -> dict[str, JudoDevice]:
    return parse_devices(build_response(**kwargs))


@pytest.fixture
def mock_api():
    """Patch the cloud API."""
    with (
        patch(
            "custom_components.judo_isoft_save_plus.api.JudoApi.login",
            new=AsyncMock(return_value="token"),
        ) as login,
        patch(
            "custom_components.judo_isoft_save_plus.api.JudoApi.get_devices",
            new=AsyncMock(side_effect=lambda: devices()),
        ) as get_devices,
        patch(
            "custom_components.judo_isoft_save_plus.api.JudoApi.get_error_messages",
            new=AsyncMock(return_value=[]),
        ) as get_errors,
        patch(
            "custom_components.judo_isoft_save_plus.api.JudoApi.write",
            new=AsyncMock(return_value=None),
        ) as write,
    ):
        yield {
            "login": login,
            "get_devices": get_devices,
            "get_error_messages": get_errors,
            "write": write,
        }
