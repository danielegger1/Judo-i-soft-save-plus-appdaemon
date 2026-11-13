"""The JUDO i-soft SAFE+ integration (myjudo.eu cloud)."""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import JudoApi
from .const import CONF_LEGACY_MIGRATION, DOMAIN
from .coordinator import JudoCoordinator
from .legacy import async_migrate, read_seed

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type JudoConfigEntry = ConfigEntry[JudoCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: JudoConfigEntry) -> bool:
    """Set up JUDO i-soft SAFE+ from a config entry."""
    api = JudoApi(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = JudoCoordinator(hass, entry, api)
    await coordinator.async_load_storage()

    # chosen in the config flow: continue history of the AppDaemon/MQTT bridge
    legacy = entry.data.get(CONF_LEGACY_MIGRATION)
    if legacy:
        coordinator.legacy_seed = read_seed(hass, legacy["entities"])

    await coordinator.async_config_entry_first_refresh()

    if legacy:
        serial = next(iter(coordinator.data.devices))
        taken_over = await async_migrate(
            hass,
            entry,
            serial,
            legacy["entities"],
            legacy.get("remove", []),
            legacy.get("device_id"),
        )
        # only once
        data = {k: v for k, v in entry.data.items() if k != CONF_LEGACY_MIGRATION}
        hass.config_entries.async_update_entry(entry, data=data)
        persistent_notification.async_create(
            hass,
            _migration_message(hass, taken_over),
            title="JUDO i-soft SAFE+",
            notification_id=f"{DOMAIN}_legacy_migration",
        )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: JudoConfigEntry) -> None:
    """Reload after the options were changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: JudoConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _migration_message(hass: HomeAssistant, entity_ids: list[str]) -> str:
    lines = "\n".join(f"- `{entity_id}`" for entity_id in sorted(entity_ids))
    if hass.config.language.startswith("de"):
        return (
            f"Verlauf und Langzeitstatistik von {len(entity_ids)} Entitäten der "
            f"AppDaemon/MQTT-Bridge wurden übernommen:\n\n{lines}"
        )
    return (
        f"History and long-term statistics of {len(entity_ids)} entities of the "
        f"AppDaemon/MQTT bridge were taken over:\n\n{lines}"
    )
