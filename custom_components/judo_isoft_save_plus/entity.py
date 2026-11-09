"""Base entity for JUDO i-soft SAFE+."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import JudoCoordinator


class JudoEntity(CoordinatorEntity[JudoCoordinator]):
    """Entity bound to one device (serial number)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: JudoCoordinator,
        serial: str,
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.serial = serial
        self.entity_description = description
        # Language independent; the old MQTT bridge used the translated
        # entity name, so switching the language created new entities.
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=f"JUDO {MODEL}",
            serial_number=serial,
            configuration_url="https://www.myjudo.eu",
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.serial in self.coordinator.data.devices
        )

    @property
    def _values(self) -> dict[str, Any]:
        return self.coordinator.data.devices[self.serial].values

    def _get(self, key: str | None = None) -> Any:
        return self._values.get(key or self.entity_description.key)
