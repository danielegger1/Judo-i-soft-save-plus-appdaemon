"""Binary sensors for JUDO i-soft SAFE+."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import JudoConfigEntry
from .entity import JudoEntity

REGENERATION = BinarySensorEntityDescription(
    key="regeneration_running",
    translation_key="regeneration_running",
    device_class=BinarySensorDeviceClass.RUNNING,
)

CLOUD = BinarySensorEntityDescription(
    key="cloud_connection",
    translation_key="cloud_connection",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: JudoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for serial in coordinator.data.devices:
        entities.append(JudoBinarySensor(coordinator, serial, REGENERATION))
        entities.append(JudoCloudSensor(coordinator, serial, CLOUD))
    async_add_entities(entities)


class JudoBinarySensor(JudoEntity, BinarySensorEntity):
    """Regeneration running."""

    @property
    def is_on(self) -> bool | None:
        return self._get()


class JudoCloudSensor(JudoEntity, BinarySensorEntity):
    """Connection to myjudo.eu (replaces the old "Cloud_Verbindung" sensor)."""

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success
