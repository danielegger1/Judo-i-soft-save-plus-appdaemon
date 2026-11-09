"""Switches for JUDO i-soft SAFE+."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import JudoConfigEntry
from .const import REG_WATER_LOCK_CLOSE, REG_WATER_LOCK_OPEN
from .entity import JudoEntity

WATER_LOCK = SwitchEntityDescription(
    key="water_lock",
    translation_key="water_lock",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JudoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        JudoWaterLockSwitch(coordinator, serial, WATER_LOCK)
        for serial in coordinator.data.devices
    )


class JudoWaterLockSwitch(JudoEntity, SwitchEntity):
    """Leakage protection valve: on = water supply closed."""

    @property
    def is_on(self) -> bool | None:
        return self._get()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(self.serial, REG_WATER_LOCK_CLOSE)
        self.coordinator.set_value(self.serial, "water_lock", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write(self.serial, REG_WATER_LOCK_OPEN)
        self.coordinator.set_value(self.serial, "water_lock", False)
