"""Sensors for JUDO i-soft SAFE+."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import JudoConfigEntry
from .entity import JudoEntity

HARDNESS = "°dH"

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="total_water",
        translation_key="total_water",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key="total_softwater",
        translation_key="total_softwater",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key="total_hardwater",
        translation_key="total_hardwater",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key="water_today",
        translation_key="water_today",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="water_yesterday",
        translation_key="water_yesterday",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
    ),
    SensorEntityDescription(
        key="water_flow",
        translation_key="water_flow",
        native_unit_of_measurement=UnitOfVolumeFlowRate.LITERS_PER_HOUR,
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="salt_range",
        translation_key="salt_range",
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
    ),
    SensorEntityDescription(
        key="input_hardness",
        translation_key="input_hardness",
        native_unit_of_measurement=HARDNESS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="next_revision",
        translation_key="next_revision",
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="battery",
        translation_key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="regenerations",
        translation_key="regenerations",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="hours_since_regeneration",
        translation_key="hours_since_regeneration",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
    ),
    SensorEntityDescription(
        key="avg_regeneration_interval",
        translation_key="avg_regeneration_interval",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        suggested_display_precision=1,
    ),
)

MESSAGE_SENSOR = SensorEntityDescription(
    key="last_message",
    translation_key="last_message",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JudoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for serial in coordinator.data.devices:
        entities.extend(JudoSensor(coordinator, serial, desc) for desc in SENSORS)
    # The fault memory belongs to the account, not to a single device
    first_serial = next(iter(coordinator.data.devices))
    entities.append(JudoMessageSensor(coordinator, first_serial, MESSAGE_SENSOR))
    async_add_entities(entities)


class JudoSensor(JudoEntity, SensorEntity):
    """Value sensor."""

    @property
    def native_value(self) -> Any:
        return self._get()


class JudoMessageSensor(JudoEntity, SensorEntity):
    """Newest warning/error from the fault memory."""

    @property
    def available(self) -> bool:
        # keep the last message visible while the cloud is unreachable
        return self.coordinator.data is not None

    @property
    def native_value(self) -> str | None:
        msg = self.coordinator.data.last_message
        return msg["message"] if msg else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        msg = self.coordinator.data.last_message
        if not msg:
            return None
        return {k: v for k, v in msg.items() if k != "message"}
