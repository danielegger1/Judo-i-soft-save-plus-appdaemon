"""Buttons for JUDO i-soft SAFE+."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import JudoConfigEntry
from .const import REG_START_REGENERATION
from .entity import JudoEntity

START_REGENERATION = ButtonEntityDescription(
    key="start_regeneration",
    translation_key="start_regeneration",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JudoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        JudoRegenerationButton(coordinator, serial, START_REGENERATION)
        for serial in coordinator.data.devices
    )


class JudoRegenerationButton(JudoEntity, ButtonEntity):
    """Start a regeneration.

    The old MQTT bridge used a switch, and switching it *off* also started
    a regeneration. A button cannot be misused that way.
    """

    async def async_press(self) -> None:
        await self.coordinator.async_write(self.serial, REG_START_REGENERATION)
        self.coordinator.set_value(self.serial, "regeneration_running", True)
