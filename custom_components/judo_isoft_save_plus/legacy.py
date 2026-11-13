"""Take over history and statistics from the old AppDaemon/MQTT bridge.

Home Assistant stores history (states) and long-term statistics by
entity_id. The old bridge created MQTT entities; if the entities of this
integration get exactly the same entity_ids, history and statistics simply
continue. Renaming afterwards does not work (the recorder refuses to rename
onto an entity_id that already has statistics), so the new entities are
registered with the old entity_ids *before* they are added for the first
time:

1. find the MQTT entities of the old bridge (unique_id "<NAME>-<LOCATION>_<name>")
2. remove them from the entity registry (history stays in the database;
   a running MQTT integration also clears the retained discovery topic,
   so they are not rediscovered)
3. register the new entities with the old entity_ids
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MQTT = "mqtt"
# The old bridge publishes every 20 s; anything newer is a running bridge
ACTIVE_WITHIN = timedelta(minutes=2)
# Waiting for the MQTT entities to be removed
REMOVE_TIMEOUT = 10.0
MIN_MATCHES = 3

# Old entity name (DE and ENG language of the bridge) -> new key.
# None: no counterpart with the same entity domain (history can not be
# continued, the old entity is only removed).
LEGACY_NAMES: dict[str, str | None] = {
    # DE
    "Revision_in": "next_revision",
    "Gesamtwasserverbrauch": "total_water",
    "Gesamtweichwasseranteil": "total_softwater",
    "Gesamthartwasseranteil": "total_hardwater",
    # names of modified bridge versions
    "Gesamtweichwasserverbrauch": "total_softwater",
    "Gesamthartwasserverbrauch": "total_hardwater",
    "Salzvorrat": "salt_stock",
    "Salzreichweite": "salt_range",
    "Wunschwasserhaerte": "output_hardness",
    "Rohwasserhaerte": "input_hardness",
    "Wasserdurchflussmenge": "water_flow",
    "Batterierestkapazitaet": "battery",
    "Anzahl_Regenerationen": "regenerations",
    "Wasser_absperren": "water_lock",
    "Regeneration": None,  # switch -> now button + binary_sensor
    "Sleepmode": "sleepmode",
    "Verbrauch_Heute": "water_today",
    "Verbrauch_Gestern": "water_yesterday",
    "Meldung": "last_message",
    "max_Entnahmedauer": "extraction_time",
    "max_Wasserdurchfluss": "max_waterflow",
    "max_Entnahmemenge": "extraction_quantity",
    "Urlaubsmodus": "holiday_mode",
    "Stunden_seit_letzter_Regeneration": "hours_since_regeneration",
    "durchschn_Regenerationsintervall": "avg_regeneration_interval",
    "Cloud_Verbindung": None,  # sensor -> now binary_sensor
    # ENG
    "Total_water_consumption": "total_water",
    "Total_softwater_proportion": "total_softwater",
    "Total_hardwater_proportion": "total_hardwater",
    "Salt_stock": "salt_stock",
    "Salt_range": "salt_range",
    "Output_hardness": "output_hardness",
    "Input_hardness": "input_hardness",
    "Water_flow_rate": "water_flow",
    "Battery_capacity": "battery",
    "Regeneration_count": "regenerations",
    "Water_lock": "water_lock",
    "Consumption_today": "water_today",
    "Consumption_yesterday": "water_yesterday",
    "Notification": "last_message",
    # the English names of the old bridge did not match their meaning
    "max_withdrawal_quantity": "extraction_time",
    "max_volumetric_flow_rate": "max_waterflow",
    "max_consumption": "extraction_quantity",
    "Holiday_mode": "holiday_mode",
    "Hours_since_last_regeneration": "hours_since_regeneration",
    "Avg_regeneration_interval": "avg_regeneration_interval",
    "Cloud_connection": None,
}
# longest first: "..._Stunden_seit_letzter_Regeneration" must not match "Regeneration"
_SORTED_NAMES = sorted(LEGACY_NAMES, key=len, reverse=True)

# entity domain of every new key
NEW_DOMAINS: dict[str, str] = {
    "next_revision": "sensor",
    "total_water": "sensor",
    "total_softwater": "sensor",
    "total_hardwater": "sensor",
    "salt_stock": "number",
    "salt_range": "sensor",
    "output_hardness": "number",
    "input_hardness": "sensor",
    "water_flow": "sensor",
    "battery": "sensor",
    "regenerations": "sensor",
    "water_lock": "switch",
    "sleepmode": "number",
    "water_today": "sensor",
    "water_yesterday": "sensor",
    "last_message": "sensor",
    "extraction_time": "number",
    "max_waterflow": "number",
    "extraction_quantity": "number",
    "holiday_mode": "select",
    "hours_since_regeneration": "sensor",
    "avg_regeneration_interval": "sensor",
}


@dataclass
class LegacyEntity:
    """One entity of the old bridge."""

    entity_id: str
    legacy_name: str
    key: str | None  # new key, None = not transferable
    # The bridge ran with DE and ENG names at different times, so the same
    # value can exist twice (e.g. "Water_lock" and "Wasser_absperren").
    # Only one of them can hand over its entity_id, the other is removed.
    duplicate: bool = False

    @property
    def transferable(self) -> bool:
        return (
            self.key is not None
            and not self.duplicate
            and self.entity_id.split(".", 1)[0] == NEW_DOMAINS[self.key]
        )


@dataclass
class LegacyGroup:
    """All entities of one old bridge instance (one "NAME-LOCATION")."""

    client_id: str
    device_id: str | None
    entities: list[LegacyEntity]

    @property
    def transferable(self) -> list[LegacyEntity]:
        return [e for e in self.entities if e.transferable]

    def migration(self) -> dict[str, str]:
        """new key -> old entity_id."""
        return {e.key: e.entity_id for e in self.transferable if e.key}


def _split_unique_id(unique_id: str) -> tuple[str, str] | None:
    for name in _SORTED_NAMES:
        suffix = "_" + name
        if unique_id.endswith(suffix) and len(unique_id) > len(suffix):
            return unique_id[: -len(suffix)], name
    return None


def find_legacy_groups(hass: HomeAssistant) -> list[LegacyGroup]:
    """Find the MQTT entities created by the old AppDaemon/MQTT bridge."""
    ent_reg = er.async_get(hass)
    groups: dict[str, LegacyGroup] = {}
    for entry in ent_reg.entities.values():
        if entry.platform != MQTT or not entry.unique_id:
            continue
        split = _split_unique_id(entry.unique_id)
        if split is None:
            continue
        client_id, legacy_name = split
        # client_id of the old bridge is "<NAME>-<LOCATION>"
        if "-" not in client_id:
            continue
        group = groups.setdefault(client_id, LegacyGroup(client_id, entry.device_id, []))
        if group.device_id is None:
            group.device_id = entry.device_id
        group.entities.append(
            LegacyEntity(entry.entity_id, legacy_name, LEGACY_NAMES[legacy_name])
        )

    result = []
    for group in groups.values():
        # one entity per new key: the most recently updated one (the name
        # set the bridge used last) takes over, the others are removed
        seen: set[str] = set()
        for ent in sorted(
            group.entities, key=lambda e: _recency(hass, e.entity_id), reverse=True
        ):
            if ent.key is None:
                continue
            if ent.key in seen:
                ent.duplicate = True
            else:
                seen.add(ent.key)
        group.entities.sort(key=lambda e: e.entity_id)
        if len(group.transferable) >= MIN_MATCHES and "total_water" in seen:
            result.append(group)
    return sorted(result, key=lambda g: g.client_id)


def _recency(hass: HomeAssistant, entity_id: str) -> tuple[int, float]:
    """Sort key: available states first, then the newest one."""
    state = hass.states.get(entity_id)
    if state is None:
        return (0, 0.0)
    available = state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    return (int(available), state.last_reported.timestamp())


def bridge_still_running(hass: HomeAssistant, group: LegacyGroup) -> bool:
    """True if the old bridge still publishes values."""
    now = dt_util.utcnow()
    for ent in group.entities:
        state = hass.states.get(ent.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        if now - state.last_reported < ACTIVE_WITHIN:
            return True
    return False


def _number(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id or (state := hass.states.get(entity_id)) is None:
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def read_seed(hass: HomeAssistant, migration: dict[str, str]) -> dict[str, Any]:
    """Read the last values of the old bridge to continue its counters."""
    seed: dict[str, Any] = {}
    today = _number(hass, migration.get("water_today"))
    if today is not None:
        state = hass.states.get(migration["water_today"])
        assert state is not None
        if dt_util.as_local(state.last_changed).date() == dt_util.now().date():
            seed["water_today_l"] = int(today)
    yesterday = _number(hass, migration.get("water_yesterday"))
    if yesterday is not None:
        seed["water_yesterday_l"] = int(yesterday)
    avg = _number(hass, migration.get("avg_regeneration_interval"))
    if avg:
        seed["reg_mean_h"] = avg
    hours = _number(hass, migration.get("hours_since_regeneration"))
    if hours is not None:
        seed["hours_since_regeneration"] = hours
    return seed


async def async_migrate(
    hass: HomeAssistant,
    entry: ConfigEntry,
    serial: str,
    migration: dict[str, str],
    remove_entity_ids: list[str],
    legacy_device_id: str | None,
) -> list[str]:
    """Hand over the entity_ids. Returns the entity_ids taken over."""
    ent_reg = er.async_get(hass)
    todo: dict[str, str] = {}

    for key, old_entity_id in migration.items():
        domain = NEW_DOMAINS.get(key)
        if domain is None or old_entity_id.split(".", 1)[0] != domain:
            continue
        if ent_reg.async_get_entity_id(domain, DOMAIN, f"{serial}_{key}"):
            continue  # new entity already exists, nothing to take over
        old = ent_reg.async_get(old_entity_id)
        if old is not None and old.platform != MQTT:
            _LOGGER.warning(
                "%s is no longer an MQTT entity, not taken over", old_entity_id
            )
            continue
        todo[key] = old_entity_id

    to_remove = set(todo.values()) | set(remove_entity_ids)
    for entity_id in to_remove:
        old = ent_reg.async_get(entity_id)
        if old is not None and old.platform == MQTT:
            ent_reg.async_remove(entity_id)

    # A loaded MQTT entity removes itself (and its state) asynchronously and
    # clears its retained discovery topic. Wait for it, so the entity_id is
    # free and the old entity does not remove the new state afterwards.
    if MQTT in hass.config.components:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REMOVE_TIMEOUT
        while any(hass.states.get(i) for i in to_remove) and loop.time() < deadline:
            await asyncio.sleep(0.1)
    for entity_id in to_remove:
        if hass.states.get(entity_id) is not None:
            hass.states.async_remove(entity_id)

    taken_over: list[str] = []
    for key, old_entity_id in todo.items():
        domain, object_id = old_entity_id.split(".", 1)
        new = ent_reg.async_get_or_create(
            domain,
            DOMAIN,
            f"{serial}_{key}",
            suggested_object_id=object_id,
            config_entry=entry,
        )
        if new.entity_id == old_entity_id:
            taken_over.append(old_entity_id)
        else:
            _LOGGER.warning(
                "Could not take over %s (got %s)", old_entity_id, new.entity_id
            )

    # remove the now empty MQTT device of the old bridge
    if legacy_device_id:
        dev_reg = dr.async_get(hass)
        if dev_reg.async_get(legacy_device_id) and not er.async_entries_for_device(
            ent_reg, legacy_device_id, include_disabled_entities=True
        ):
            dev_reg.async_remove_device(legacy_device_id)

    _LOGGER.info(
        "Took over history of %d entities from the AppDaemon/MQTT bridge",
        len(taken_over),
    )
    return taken_over
