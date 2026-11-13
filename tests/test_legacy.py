"""Takeover of the AppDaemon/MQTT bridge history."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.judo_isoft_save_plus.const import (
    CONF_LEGACY_GROUP,
    CONF_LEGACY_MIGRATION,
    CONF_MIGRATE_HISTORY,
    DOMAIN,
)
from custom_components.judo_isoft_save_plus.legacy import find_legacy_groups

from .conftest import SERIAL

CLIENT_ID = "Judo_isoftsaveplus-Keller"
OLD = {
    # name: (domain, state, unit)
    "Gesamtwasserverbrauch": ("sensor", "123.4", "m³"),
    "Gesamtweichwasseranteil": ("sensor", "99.9", "m³"),
    "Salzvorrat": ("number", "20.0", "kg"),
    "Verbrauch_Heute": ("sensor", "150", "L"),
    "Verbrauch_Gestern": ("sensor", "321", "L"),
    "Stunden_seit_letzter_Regeneration": ("sensor", "5", "h"),
    "durchschn_Regenerationsintervall": ("sensor", "30.5", "h"),
    "Wasser_absperren": ("switch", "off", None),
    "Regeneration": ("switch", "off", None),
    "Cloud_Verbindung": ("sensor", "Verbunden", None),
}


def _old_id(name: str) -> str:
    domain = OLD[name][0]
    return f"{domain}.judo_isoftsaveplus_keller_{name.lower()}"


def _add_legacy(hass: HomeAssistant, client_id: str = CLIENT_ID) -> str:
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        identifiers={("mqtt", f"[{client_id}]")},
        name=client_id,
    )
    ent_reg = er.async_get(hass)
    for name, (domain, state, unit) in OLD.items():
        entry = ent_reg.async_get_or_create(
            domain,
            "mqtt",
            f"{client_id}_{name}",
            suggested_object_id=(
                f"judo_isoftsaveplus_keller_{name.lower()}"
                if client_id == CLIENT_ID
                else f"{client_id.lower()}_{name.lower()}"
            ),
            config_entry=mqtt_entry,
            device_id=device.id,
        )
        hass.states.async_set(
            entry.entity_id, state, {"unit_of_measurement": unit} if unit else {}
        )
    return device.id


async def test_find_groups(hass: HomeAssistant) -> None:
    _add_legacy(hass)
    # unrelated MQTT entity
    er.async_get(hass).async_get_or_create("sensor", "mqtt", "foo-bar_Regeneration")
    groups = find_legacy_groups(hass)
    assert len(groups) == 1
    group = groups[0]
    assert group.client_id == CLIENT_ID
    migration = group.migration()
    assert migration["total_water"] == _old_id("Gesamtwasserverbrauch")
    assert migration["hours_since_regeneration"] == _old_id(
        "Stunden_seit_letzter_Regeneration"
    )
    assert migration["salt_stock"] == _old_id("Salzvorrat")
    # different entity domain -> not transferable
    assert "Regeneration" not in {e.legacy_name for e in group.transferable}
    assert {e.legacy_name for e in group.entities if not e.transferable} == {
        "Regeneration",
        "Cloud_Verbindung",
    }


async def _start_flow(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user", CONF_PASSWORD: "pw"}
    )


async def test_takeover(
    hass: HomeAssistant, mock_api, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-09 10:00:00+00:00")
    device_id = _add_legacy(hass)

    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "legacy"
    placeholders = result["description_placeholders"]
    assert placeholders["client_id"] == CLIENT_ID
    assert placeholders["count"] == "8"
    assert f"`{_old_id('Gesamtwasserverbrauch')}`" in placeholders["table"]
    assert "123.4 m³" in placeholders["table"]
    assert _old_id("Cloud_Verbindung") in placeholders["skipped"]

    # old bridge still publishes -> refuse
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MIGRATE_HISTORY: True}
    )
    assert result["errors"] == {"base": "bridge_running"}

    freezer.tick(timedelta(minutes=5))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MIGRATE_HISTORY: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert CONF_LEGACY_MIGRATION not in entry.data

    ent_reg = er.async_get(hass)
    for name, key, domain in (
        ("Gesamtwasserverbrauch", "total_water", "sensor"),
        ("Verbrauch_Heute", "water_today", "sensor"),
        ("Salzvorrat", "salt_stock", "number"),
        ("Wasser_absperren", "water_lock", "switch"),
    ):
        reg = ent_reg.async_get(_old_id(name))
        assert reg is not None, name
        assert reg.platform == DOMAIN
        assert reg.unique_id == f"{SERIAL}_{key}"
        assert reg.domain == domain

    assert hass.states.get(_old_id("Gesamtwasserverbrauch")).state == "123.456"
    # counters continued
    assert hass.states.get(_old_id("Verbrauch_Heute")).state == "150"
    assert hass.states.get(_old_id("Verbrauch_Gestern")).state == "321"
    assert hass.states.get(_old_id("durchschn_Regenerationsintervall")).state == "30.5"
    assert hass.states.get(_old_id("Stunden_seit_letzter_Regeneration")).state == "5"

    # not transferable entities, their states and the old device are gone
    assert ent_reg.async_get(_old_id("Regeneration")) is None
    assert hass.states.get(_old_id("Cloud_Verbindung")) is None
    assert not [e for e in ent_reg.entities.values() if e.platform == "mqtt"]
    assert dr.async_get(hass).async_get(device_id) is None
    # new-only entities get normal ids
    assert ent_reg.async_get_entity_id("button", DOMAIN, f"{SERIAL}_start_regeneration")

    # reload does not migrate again
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(_old_id("Verbrauch_Heute")).state == "150"


async def test_takeover_declined(
    hass: HomeAssistant, mock_api, freezer: FrozenDateTimeFactory
) -> None:
    _add_legacy(hass)
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MIGRATE_HISTORY: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get(_old_id("Gesamtwasserverbrauch")).platform == "mqtt"
    new_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_total_water")
    assert new_id and new_id != _old_id("Gesamtwasserverbrauch")


async def test_no_legacy(hass: HomeAssistant, mock_api) -> None:
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_pick_group(
    hass: HomeAssistant, mock_api, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-09 10:00:00+00:00")
    _add_legacy(hass)
    _add_legacy(hass, "Judo-Garage")
    freezer.tick(timedelta(minutes=5))
    result = await _start_flow(hass)
    assert result["step_id"] == "legacy_pick"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LEGACY_GROUP: "judo_isoftsaveplus_keller"}
    )
    assert result["step_id"] == "legacy"
    assert result["description_placeholders"]["client_id"] == CLIENT_ID
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MIGRATE_HISTORY: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get(_old_id("Gesamtwasserverbrauch")).platform == DOMAIN
    # the other bridge is untouched
    assert ent_reg.async_get("sensor.judo_garage_gesamtwasserverbrauch").platform == "mqtt"


async def test_duplicate_names_are_removed(
    hass: HomeAssistant, mock_api, freezer: FrozenDateTimeFactory
) -> None:
    """The bridge ran with ENG names first, then DE: both must be handled."""
    freezer.move_to("2026-11-09 10:00:00+00:00")
    _add_legacy(hass)
    ent_reg = er.async_get(hass)
    old_en = ent_reg.async_get_or_create(
        "switch", "mqtt", f"{CLIENT_ID}_Water_lock",
        suggested_object_id="isoftsafeplus_home_water_lock",
    )
    hass.states.async_set(old_en.entity_id, "unavailable", {"restored": True})

    group = find_legacy_groups(hass)[0]
    dup = next(e for e in group.entities if e.legacy_name == "Water_lock")
    assert dup.duplicate and not dup.transferable
    # the DE entity (still has a value) takes over
    assert group.migration()["water_lock"] == _old_id("Wasser_absperren")

    freezer.tick(timedelta(minutes=5))
    result = await _start_flow(hass)
    assert old_en.entity_id in result["description_placeholders"]["skipped"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MIGRATE_HISTORY: True}
    )
    await hass.async_block_till_done()
    assert ent_reg.async_get(old_en.entity_id) is None
    assert hass.states.get(old_en.entity_id) is None
    assert ent_reg.async_get(_old_id("Wasser_absperren")).platform == DOMAIN
