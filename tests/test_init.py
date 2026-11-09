"""Integration tests."""

from __future__ import annotations


from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.judo_isoft_save_plus.api import JudoAuthError, JudoConnectionError
from custom_components.judo_isoft_save_plus.const import (
    CONF_SODIUM_CHECK,
    CONF_SODIUM_INPUT,
    CONF_SODIUM_LIMIT,
    DOMAIN,
    EVENT_MESSAGE,
)
from custom_components.judo_isoft_save_plus.number import limit_hardness_by_sodium

from .conftest import SERIAL, devices


def _entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user",
        data={CONF_USERNAME: "user", CONF_PASSWORD: "pw"},
        options=options or {},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _eid(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")
    assert entity_id, key
    return entity_id


async def test_setup_and_values(hass: HomeAssistant, mock_api) -> None:
    entry = _entry()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    def state(platform: str, key: str) -> str:
        return hass.states.get(_eid(hass, platform, key)).state

    assert state("sensor", "total_water") == "123.456"
    assert state("sensor", "total_softwater") == "100.0"
    assert state("sensor", "total_hardwater") == "23.456"
    assert state("sensor", "water_today") == "0"
    assert state("sensor", "input_hardness") == "20"
    assert state("sensor", "water_flow") == "1200"
    assert state("sensor", "salt_range") == "40"
    assert state("sensor", "next_revision") == "100"
    assert state("sensor", "battery") == "87"
    assert state("sensor", "regenerations") == "50"
    assert state("sensor", "hours_since_regeneration") == "unknown"
    assert state("number", "output_hardness") == "8"
    assert state("number", "salt_stock") == "25.0"
    assert state("number", "sleepmode") == "2"
    assert state("number", "max_waterflow") == "2500"
    assert state("number", "extraction_quantity") == "300"
    assert state("number", "extraction_time") == "30"
    assert state("switch", "water_lock") == "off"
    assert state("select", "holiday_mode") == "off"
    assert state("binary_sensor", "regeneration_running") == "off"
    assert state("binary_sensor", "cloud_connection") == "on"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_consumption_and_regeneration(
    hass: HomeAssistant, mock_api, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-09 10:00:00+00:00")
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data

    mock_api["get_devices"].side_effect = lambda: devices(total_l=123556, regenerations=51)
    await coordinator.async_refresh()
    assert hass.states.get(_eid(hass, "sensor", "water_today")).state == "100"
    assert hass.states.get(_eid(hass, "sensor", "hours_since_regeneration")).state == "0"

    # decreasing total is ignored
    mock_api["get_devices"].side_effect = lambda: devices(total_l=1000, regenerations=51)
    await coordinator.async_refresh()
    assert hass.states.get(_eid(hass, "sensor", "total_water")).state == "123.556"

    # next day, 30 h after the regeneration
    freezer.move_to("2026-11-10 16:00:00+00:00")
    mock_api["get_devices"].side_effect = lambda: devices(total_l=123756, regenerations=52)
    await coordinator.async_refresh()
    assert hass.states.get(_eid(hass, "sensor", "water_yesterday")).state == "100"
    assert hass.states.get(_eid(hass, "sensor", "water_today")).state == "200"
    assert hass.states.get(_eid(hass, "sensor", "avg_regeneration_interval")).state == "30.0"


async def test_connection_error_keeps_running(hass: HomeAssistant, mock_api) -> None:
    entry = _entry()
    await _setup(hass, entry)
    mock_api["get_devices"].side_effect = JudoConnectionError("timeout")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(_eid(hass, "sensor", "total_water")).state == STATE_UNAVAILABLE
    assert hass.states.get(_eid(hass, "binary_sensor", "cloud_connection")).state == "off"

    mock_api["get_devices"].side_effect = lambda: devices()
    await entry.runtime_data.async_refresh()
    assert hass.states.get(_eid(hass, "sensor", "total_water")).state == "123.456"
    assert hass.states.get(_eid(hass, "binary_sensor", "cloud_connection")).state == "on"


async def test_auth_error_starts_reauth(hass: HomeAssistant, mock_api) -> None:
    mock_api["get_devices"].side_effect = JudoAuthError("bad")
    entry = _entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_messages(hass: HomeAssistant, mock_api) -> None:
    mock_api["get_error_messages"].return_value = [
        {"id": 5, "type": "w", "error": "28", "ts_sort": "2026-11-01 10:00:00.000000"}
    ]
    entry = _entry()
    events = async_capture_events(hass, EVENT_MESSAGE)
    await _setup(hass, entry)
    # old entry on first start: shown, but no alert
    msg = hass.states.get(_eid(hass, "sensor", "last_message"))
    assert msg.state == "Check salt storage."
    assert msg.attributes["code"] == "28"
    assert msg.attributes["timestamp"] == "2026-11-01 10:00:00"
    assert events == []

    mock_api["get_error_messages"].return_value = [
        {"id": 7, "type": "e", "error": "1", "ts_sort": "x"},
        {"id": 6, "type": "w", "error": "999", "ts_sort": "x"},
        {"id": 5, "type": "w", "error": "28", "ts_sort": "x"},
    ]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["id"] for e in events] == [6, 7]
    assert events[0].data["message"] == "Unknown warning (JUDO error code 999)"
    assert hass.states.get(_eid(hass, "sensor", "last_message")).state == (
        "Regeneration drive defective"
    )

    # nothing new -> no event
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 2


async def test_messages_empty_first(hass: HomeAssistant, mock_api) -> None:
    entry = _entry()
    events = async_capture_events(hass, EVENT_MESSAGE)
    await _setup(hass, entry)
    mock_api["get_error_messages"].return_value = [{"id": 1, "type": "w", "error": "24"}]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_commands(hass: HomeAssistant, mock_api) -> None:
    entry = _entry()
    await _setup(hass, entry)
    write = mock_api["write"]

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, "number", "output_hardness"), "value": 6},
        blocking=True,
    )
    assert write.call_args.args[1:] == (60, "06")
    assert hass.states.get(_eid(hass, "number", "output_hardness")).state == "6"

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, "number", "salt_stock"), "value": 30},
        blocking=True,
    )
    assert write.call_args.args[1:] == (94, "3075")

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, "number", "max_waterflow"), "value": 3000},
        blocking=True,
    )
    assert write.call_args.args[1:] == (75, "B80B")

    write.reset_mock()
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, "number", "sleepmode"), "value": 3},
        blocking=True,
    )
    assert [c.args[1:] for c in write.call_args_list] == [(171, "3"), (171, "")]

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _eid(hass, "switch", "water_lock")}, blocking=True
    )
    assert write.call_args.args[1:] == (72, "")
    assert hass.states.get(_eid(hass, "switch", "water_lock")).state == "on"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, "switch", "water_lock")}, blocking=True
    )
    assert write.call_args.args[1:] == (73, "")

    await hass.services.async_call(
        "button", "press",
        {"entity_id": _eid(hass, "button", "start_regeneration")},
        blocking=True,
    )
    assert write.call_args.args[1:] == (65, "")

    write.reset_mock()
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _eid(hass, "select", "holiday_mode"), "option": "mode_1"},
        blocking=True,
    )
    assert write.call_args.args[1:] == (77, "3")
    write.reset_mock()
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _eid(hass, "select", "holiday_mode"), "option": "off"},
        blocking=True,
    )
    assert [c.args[1:] for c in write.call_args_list] == [(73, ""), (77, "0")]

    # out of range value is rejected by HA before it reaches the API
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": _eid(hass, "number", "max_waterflow"), "value": 5000},
            blocking=True,
        )

    write.side_effect = JudoConnectionError("timeout")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press",
            {"entity_id": _eid(hass, "button", "start_regeneration")},
            blocking=True,
        )


def test_sodium_limit() -> None:
    # 20 °dH raw, 10 mg/L: 8 °dH -> 108.4 mg/L, ok
    assert limit_hardness_by_sodium(8, 20, 10, 200) == (8, 108.4)
    # 1 °dH -> 165.8 ok; limit 100 -> needs >= 9 °dH
    assert limit_hardness_by_sodium(1, 20, 10, 100)[0] == 10
    assert limit_hardness_by_sodium(1, 20, 10, 100)[1] < 100


async def test_sodium_check(hass: HomeAssistant, mock_api) -> None:
    entry = _entry({CONF_SODIUM_CHECK: True, CONF_SODIUM_INPUT: 10, CONF_SODIUM_LIMIT: 100})
    await _setup(hass, entry)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": _eid(hass, "number", "output_hardness"), "value": 2},
        blocking=True,
    )
    assert mock_api["write"].call_args.args[1:] == (60, "0A")
