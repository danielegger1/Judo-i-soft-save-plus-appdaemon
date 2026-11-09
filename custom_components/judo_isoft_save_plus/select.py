"""Select entities for JUDO i-soft SAFE+."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import JudoConfigEntry
from .const import (
    HOLIDAY_OFF,
    HOLIDAY_OPTIONS,
    HOLIDAY_TO_RAW,
    REG_HOLIDAY_MODE,
    REG_WATER_LOCK_OPEN,
)
from .entity import JudoEntity

HOLIDAY_MODE = SelectEntityDescription(
    key="holiday_mode",
    translation_key="holiday_mode",
    options=HOLIDAY_OPTIONS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JudoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up select entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        JudoHolidaySelect(coordinator, serial, HOLIDAY_MODE)
        for serial in coordinator.data.devices
    )


class JudoHolidaySelect(JudoEntity, SelectEntity):
    """Holiday mode."""

    @property
    def current_option(self) -> str | None:
        return self._get()

    async def async_select_option(self, option: str) -> None:
        if option not in HOLIDAY_TO_RAW:
            raise ServiceValidationError(f"Invalid option {option}")
        if option == HOLIDAY_OFF:
            # open the valve first, holiday mode "waterlock" closed it
            await self.coordinator.async_write(self.serial, REG_WATER_LOCK_OPEN)
        await self.coordinator.async_write(
            self.serial, REG_HOLIDAY_MODE, str(HOLIDAY_TO_RAW[option])
        )
        self.coordinator.set_value(self.serial, "holiday_mode", option)
