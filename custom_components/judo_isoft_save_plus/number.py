"""Number entities for JUDO i-soft SAFE+."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfMass, UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import JudoConfigEntry
from .api import int_to_le_hex
from .const import (
    CONF_LIMIT_EXTRACTION_QUANTITY,
    CONF_LIMIT_EXTRACTION_TIME,
    CONF_LIMIT_MAX_WATERFLOW,
    CONF_SODIUM_CHECK,
    CONF_SODIUM_INPUT,
    CONF_SODIUM_LIMIT,
    DEFAULT_LIMIT_EXTRACTION_QUANTITY,
    DEFAULT_LIMIT_EXTRACTION_TIME,
    DEFAULT_LIMIT_MAX_WATERFLOW,
    DEFAULT_SODIUM_CHECK,
    DEFAULT_SODIUM_INPUT,
    DEFAULT_SODIUM_LIMIT,
    REG_EXTRACTION_QUANTITY,
    REG_EXTRACTION_TIME,
    REG_MAX_WATERFLOW,
    REG_OUTPUT_HARDNESS,
    REG_SALT_STOCK,
    REG_SLEEPMODE,
    REG_WATER_LOCK_OPEN,
    SODIUM_PER_DH,
)
from .entity import JudoEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class JudoNumberDescription(NumberEntityDescription):
    """Number with register information."""

    register: int | None = None
    bits: int = 16
    scale: float = 1  # written value = displayed value * scale
    max_option: str | None = None
    max_default: float | None = None


NUMBERS: tuple[JudoNumberDescription, ...] = (
    JudoNumberDescription(
        key="output_hardness",
        translation_key="output_hardness",
        native_unit_of_measurement="°dH",
        native_min_value=1,
        native_max_value=15,
        native_step=1,
        register=REG_OUTPUT_HARDNESS,
        bits=8,
    ),
    JudoNumberDescription(
        key="salt_stock",
        translation_key="salt_stock",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        native_min_value=0,
        native_max_value=50,
        native_step=1,
        mode=NumberMode.BOX,
        register=REG_SALT_STOCK,
        scale=1000,  # grams
    ),
    JudoNumberDescription(
        key="sleepmode",
        translation_key="sleepmode",
        native_unit_of_measurement=UnitOfTime.HOURS,
        native_min_value=0,
        native_max_value=10,
        native_step=1,
    ),
    JudoNumberDescription(
        key="extraction_time",
        translation_key="extraction_time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=10,
        native_step=10,
        entity_category=EntityCategory.CONFIG,
        register=REG_EXTRACTION_TIME,
        max_option=CONF_LIMIT_EXTRACTION_TIME,
        max_default=DEFAULT_LIMIT_EXTRACTION_TIME,
    ),
    JudoNumberDescription(
        key="max_waterflow",
        translation_key="max_waterflow",
        native_unit_of_measurement="L/h",
        native_min_value=500,
        native_step=500,
        entity_category=EntityCategory.CONFIG,
        register=REG_MAX_WATERFLOW,
        max_option=CONF_LIMIT_MAX_WATERFLOW,
        max_default=DEFAULT_LIMIT_MAX_WATERFLOW,
    ),
    JudoNumberDescription(
        key="extraction_quantity",
        translation_key="extraction_quantity",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        native_min_value=100,
        native_step=100,
        entity_category=EntityCategory.CONFIG,
        register=REG_EXTRACTION_QUANTITY,
        max_option=CONF_LIMIT_EXTRACTION_QUANTITY,
        max_default=DEFAULT_LIMIT_EXTRACTION_QUANTITY,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JudoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up number entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        JudoNumber(coordinator, serial, desc)
        for serial in coordinator.data.devices
        for desc in NUMBERS
    )


def limit_hardness_by_sodium(
    desired: float, input_hardness: float, sodium_input: float, sodium_limit: float
) -> tuple[int, float]:
    """Return (hardness to set, resulting sodium in mg/L).

    Softening replaces calcium/magnesium by sodium (about 8.2 mg/L per °dH).
    If the desired hardness would exceed the sodium limit, the lowest
    hardness that keeps the limit is returned instead.
    """
    sodium = round((input_hardness - desired) * SODIUM_PER_DH + sodium_input, 1)
    if sodium < sodium_limit:
        return int(desired), sodium
    limited = math.ceil(input_hardness - (sodium_limit - sodium_input) / SODIUM_PER_DH)
    limited = max(int(desired), limited)
    return limited, round((input_hardness - limited) * SODIUM_PER_DH + sodium_input, 1)


class JudoNumber(JudoEntity, NumberEntity):
    """Writable register."""

    entity_description: JudoNumberDescription

    @property
    def native_max_value(self) -> float:
        desc = self.entity_description
        if desc.max_option:
            return float(
                self.coordinator.config_entry.options.get(
                    desc.max_option, desc.max_default
                )
            )
        return desc.native_max_value

    @property
    def native_value(self) -> float | None:
        return self._get()

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description
        value = int(round(value))

        if desc.key == "sleepmode":
            await self._set_sleepmode(value)
        else:
            if desc.key == "output_hardness":
                value = self._apply_sodium_check(value)
            assert desc.register is not None
            try:
                data = int_to_le_hex(int(value * desc.scale), desc.bits)
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            await self.coordinator.async_write(self.serial, desc.register, data)

        self.coordinator.set_value(self.serial, desc.key, value)

    async def _set_sleepmode(self, hours: int) -> None:
        if hours == 0:
            # ends the sleep mode, leakage protection is active again
            await self.coordinator.async_write(self.serial, REG_WATER_LOCK_OPEN)
            return
        # set duration, then activate
        await self.coordinator.async_write(self.serial, REG_SLEEPMODE, str(hours))
        await self.coordinator.async_write(self.serial, REG_SLEEPMODE)

    def _apply_sodium_check(self, desired: int) -> int:
        options = self.coordinator.config_entry.options
        if not options.get(CONF_SODIUM_CHECK, DEFAULT_SODIUM_CHECK):
            return desired
        input_hardness = self._get("input_hardness")
        if input_hardness is None:
            raise HomeAssistantError(
                "Raw water hardness unknown, sodium check not possible"
            )
        limited, sodium = limit_hardness_by_sodium(
            desired,
            input_hardness,
            float(options.get(CONF_SODIUM_INPUT, DEFAULT_SODIUM_INPUT)),
            float(options.get(CONF_SODIUM_LIMIT, DEFAULT_SODIUM_LIMIT)),
        )
        if limited != desired:
            _LOGGER.warning(
                "Sodium limit would be exceeded, output hardness set to %s °dH "
                "instead of %s °dH (%s mg/L sodium)",
                limited,
                desired,
                sodium,
            )
        return limited
