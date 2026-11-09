"""Diagnostics for JUDO i-soft SAFE+."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import JudoConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, "serial", "da", "dt"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JudoConfigEntry
) -> dict[str, Any]:
    """Return diagnostics (raw registers help to debug wrong values)."""
    coordinator = entry.runtime_data
    devices = {}
    if coordinator.data:
        for index, (serial, state) in enumerate(coordinator.data.devices.items()):
            devices[f"device_{index}"] = {
                "serial": serial,
                "da": state.device.da,
                "dt": state.device.dt,
                "registers": state.device.registers,
                "values": state.values,
            }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "devices": async_redact_data(devices, TO_REDACT),
        "last_message": coordinator.data.last_message if coordinator.data else None,
    }
