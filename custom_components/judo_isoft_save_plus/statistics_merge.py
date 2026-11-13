"""Merge long-term statistics of the old AppDaemon/MQTT bridge.

The entity_id takeover in legacy.py only works while the new entities do
not exist yet. If the integration was set up before (or the bridge ran with
DE and ENG names at different times), the history is spread over several
statistic_ids, e.g.

    sensor.isoftsafeplus_home_total_water_consumption        (old, ENG)
    sensor.isoftsafeplus_home_gesamtwasserverbrauch          (old, DE)
    sensor.keller_judo_i_soft_safe_gesamtwasserverbrauch     (this integration)

and the same for the "_cost" statistics of the energy dashboard. This
module copies the hourly long-term statistics of the old ids into the id of
this integration (only for hours before it has own data) and shifts the
sums, so the energy dashboard shows one continuous series including costs.
It also points the energy dashboard to the new entities and removes the
left over registry entries of the old bridge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import DOMAIN
from .legacy import LEGACY_NAMES, MQTT, NEW_DOMAINS, _split_unique_id

_LOGGER = logging.getLogger(__name__)

RECORDER = "recorder"
ENERGY = "energy"
COST_SUFFIXES = ("_cost", "_compensation")
EPOCH = datetime(1970, 1, 2, tzinfo=dt_util.UTC)
TYPES = {"state", "sum", "mean", "min", "max", "last_reset"}

# entity_id slugs of the old names, longest first
_SLUGS = sorted(
    ((name.lower(), key) for name, key in LEGACY_NAMES.items() if key), key=lambda x: -len(x[0])
)


@dataclass
class SourceInfo:
    """One old statistic_id."""

    statistic_id: str
    first: datetime | None = None
    last: datetime | None = None
    hours: int = 0  # hours that will be copied
    amount: float | None = None  # sum of the copied hours (costs: EUR)


@dataclass
class MergePlan:
    """Old statistic_ids -> statistic_id of this integration."""

    target: str
    sources: list[SourceInfo]
    unit: str | None = None

    @property
    def is_cost(self) -> bool:
        return self.target.endswith(COST_SUFFIXES)


@dataclass
class LegacyPlan:
    """Everything the statistics transfer would do."""

    merges: list[MergePlan] = field(default_factory=list)
    energy: dict[str, str] = field(default_factory=dict)  # old -> new in prefs
    remove_entities: list[str] = field(default_factory=list)
    remove_devices: list[str] = field(default_factory=list)
    # old "_cost" sensors of the energy dashboard: their data is transferred,
    # they are only removed together with the old statistics (opt-in)
    cost_entities: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.merges or self.energy or self.remove_entities or self.remove_devices
        )

    @property
    def pending(self) -> bool:
        """Something left to do (kept old statistics alone do not count)."""
        return bool(
            any(src.hours for m in self.merges for src in m.sources)
            or self.energy
            or self.remove_entities
        )

    @property
    def old_statistic_ids(self) -> list[str]:
        return [s.statistic_id for m in self.merges for s in m.sources]


def _split_stat(statistic_id: str) -> tuple[str, str, str] | None:
    """'sensor.<prefix>_<name>[_cost]' -> (prefix, key, cost suffix)."""
    domain, _, object_id = statistic_id.partition(".")
    if domain not in ("sensor", "number"):
        return None
    suffix = ""
    for cost in COST_SUFFIXES:
        if object_id.endswith(cost):
            suffix = cost
            object_id = object_id[: -len(cost)]
            break
    for slug, key in _SLUGS:
        if object_id.endswith("_" + slug) and len(object_id) > len(slug) + 1:
            if not suffix and NEW_DOMAINS[key] != domain:
                return None
            return object_id[: -len(slug) - 1], key, suffix
    return None


def _new_entity_ids(hass: HomeAssistant, serial: str) -> dict[str, str]:
    ent_reg = er.async_get(hass)
    result = {}
    for key, domain in NEW_DOMAINS.items():
        if entity_id := ent_reg.async_get_entity_id(domain, DOMAIN, f"{serial}_{key}"):
            result[key] = entity_id
    return result


async def _rows(
    hass: HomeAssistant, statistic_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        statistics_during_period,
    )

    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        EPOCH,
        None,
        statistic_ids,
        "hour",
        None,
        TYPES,
    )


async def _metadata(
    hass: HomeAssistant, statistic_ids: set[str]
) -> dict[str, dict[str, Any]]:
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        get_metadata,
    )

    meta = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids=statistic_ids)
    )
    return {sid: dict(m[1]) for sid, m in meta.items()}


async def async_legacy_candidates(
    hass: HomeAssistant, own: set[str]
) -> dict[str, list[tuple[str, str, str]]]:
    """Old statistic ids of the bridge: prefix -> [(statistic_id, key, suffix)].

    Works from the recorder, so it also finds statistics whose MQTT entities
    were already deleted. Only prefixes with a total water statistic count.
    """
    if RECORDER not in hass.config.components:
        return {}
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        async_list_statistic_ids,
    )

    ent_reg = er.async_get(hass)
    candidates: dict[str, list[tuple[str, str, str]]] = {}
    for info in await async_list_statistic_ids(hass):
        statistic_id = info["statistic_id"]
        if info.get("source") != RECORDER or statistic_id in own:
            continue
        if (split := _split_stat(statistic_id)) is None:
            continue
        prefix, key, suffix = split
        base_entity = statistic_id[: len(statistic_id) - len(suffix)]
        if base_entity in own:
            continue  # cost statistic of a new entity
        entry = ent_reg.async_get(statistic_id)
        if entry is not None and entry.platform not in (MQTT, ENERGY):
            continue  # belongs to another integration
        candidates.setdefault(prefix, []).append((statistic_id, key, suffix))
    return {
        prefix: sorted(items)
        for prefix, items in candidates.items()
        if any(key == "total_water" for _, key, _ in items)
    }


async def async_first_setup_overview(
    hass: HomeAssistant,
    items: list[tuple[str, str, str]],
    takeover: dict[str, str],
) -> tuple[dict[str, str], list[tuple[str, SourceInfo, str | None]]]:
    """Preview for the first setup (the new entities do not exist yet).

    Returns the completed takeover (key -> old entity_id) and the other old
    statistics with their target: (target, source info, unit).

    For values whose MQTT entity is already deleted, the newest old
    statistic_id takes over (its entity_id is free), so its history,
    statistics and energy dashboard costs continue unchanged.
    """
    ent_reg = er.async_get(hass)
    ids = {sid for sid, _, _ in items}
    rows = await _rows(hass, ids | set(takeover.values()))
    meta = await _metadata(hass, ids | set(takeover.values()))

    takeover = dict(takeover)
    newest: dict[str, tuple[float, str]] = {}
    for sid, key, suffix in items:
        if suffix or key in takeover or ent_reg.async_get(sid) is not None:
            continue
        if not (src := rows.get(sid)):
            continue
        last = src[-1]["start"]
        if key not in newest or last > newest[key][0]:
            newest[key] = (last, sid)
    for key, (_, sid) in newest.items():
        takeover[key] = sid

    taken = set(takeover.values())
    result: list[tuple[str, SourceInfo, str | None]] = []
    for sid, key, suffix in items:
        if key not in takeover or sid in taken:
            continue
        target = takeover[key] + suffix
        if target == sid:
            continue  # cost statistic of a taken over entity: continues as is
        src = rows.get(sid) or []
        if not src:
            continue
        unit = (meta.get(sid) or {}).get("unit_of_measurement")
        target_unit = (meta.get(target) or {}).get("unit_of_measurement", unit)
        if unit != target_unit:
            continue
        target_start = _first_start(rows.get(target))
        copied = [r for r in src if target_start is None or r["start"] < target_start]
        result.append(
            (
                target,
                SourceInfo(
                    sid,
                    dt_util.utc_from_timestamp(src[0]["start"]),
                    dt_util.utc_from_timestamp(src[-1]["start"]),
                    len(copied),
                    _amount(copied),
                ),
                unit,
            )
        )
    return takeover, result


async def async_build_plan(hass: HomeAssistant, serial: str) -> LegacyPlan:
    """Find what can be transferred (read only)."""
    plan = LegacyPlan()
    if RECORDER not in hass.config.components:
        return plan
    new_ids = _new_entity_ids(hass, serial)
    if not new_ids:
        return plan
    own = set(new_ids.values())
    ent_reg = er.async_get(hass)
    candidates = await async_legacy_candidates(hass, own)
    prefixes = set(candidates)

    targets: dict[str, list[str]] = {}
    for prefix in prefixes:
        for statistic_id, key, suffix in candidates[prefix]:
            if key not in new_ids:
                continue
            targets.setdefault(new_ids[key] + suffix, []).append(statistic_id)

    if targets:
        all_ids = set(targets) | {s for ids in targets.values() for s in ids}
        rows = await _rows(hass, all_ids)
        meta = await _metadata(hass, all_ids)
        for target, sources in sorted(targets.items()):
            unit = (meta.get(target) or meta.get(sources[0]) or {}).get(
                "unit_of_measurement"
            )
            target_start = _first_start(rows.get(target))
            infos = []
            for source in sources:
                src_rows = rows.get(source) or []
                if (meta.get(source) or {}).get("unit_of_measurement") != unit:
                    _LOGGER.warning(
                        "Not merging %s into %s: different unit", source, target
                    )
                    continue
                if not src_rows:
                    continue
                copied = [
                    r
                    for r in src_rows
                    if target_start is None or r["start"] < target_start
                ]
                infos.append(
                    SourceInfo(
                        source,
                        dt_util.utc_from_timestamp(src_rows[0]["start"]),
                        dt_util.utc_from_timestamp(src_rows[-1]["start"]),
                        len(copied),
                        _amount(copied),
                    )
                )
            if infos:
                infos.sort(key=lambda i: i.first or EPOCH)
                plan.merges.append(MergePlan(target, infos, unit))

    # --- energy dashboard ------------------------------------------------
    replace = {s.statistic_id: m.target for m in plan.merges for s in m.sources}
    for prefix in prefixes:
        for statistic_id, key, suffix in candidates[prefix]:
            if key in new_ids and not suffix:
                replace.setdefault(statistic_id, new_ids[key])
    if ENERGY in hass.config.components and replace:
        from homeassistant.components.energy.data import (  # noqa: PLC0415
            async_get_manager,
        )

        manager = await async_get_manager(hass)
        for stat in _energy_stat_ids(manager.data or {}):
            if stat in replace:
                plan.energy[stat] = replace[stat]

    # --- left over registry entries -----------------------------------
    for entry in list(ent_reg.entities.values()):
        if entry.platform == MQTT and entry.unique_id:
            split = _split_unique_id(entry.unique_id)
            if split and slugify(split[0]) in prefixes:
                plan.remove_entities.append(entry.entity_id)
                if entry.device_id and entry.device_id not in plan.remove_devices:
                    plan.remove_devices.append(entry.device_id)
        elif entry.platform == ENERGY and entry.entity_id.endswith(COST_SUFFIXES):
            source = _split_stat(entry.entity_id)
            base = entry.entity_id.rsplit("_", 1)[0]
            if source and source[0] in prefixes and base not in own:
                state = hass.states.get(entry.entity_id)
                if base in plan.energy or state is None or state.state == STATE_UNAVAILABLE:
                    plan.cost_entities.append(entry.entity_id)
    plan.remove_entities.sort()
    plan.cost_entities.sort()
    return plan


def _amount(rows: list[dict[str, Any]]) -> float | None:
    """Total of the copied hours of a sum statistic."""
    if not rows or rows[-1].get("sum") is None or rows[0].get("sum") is None:
        return None
    first = rows[0]
    if first.get("last_reset") is not None:
        first_hour = max(min(first.get("state") or 0.0, first["sum"]), 0.0)
    else:
        first_hour = 0.0
    return round(rows[-1]["sum"] - first["sum"] + first_hour, 2)


def _first_start(rows: list[dict[str, Any]] | None) -> float | None:
    return rows[0]["start"] if rows else None


def _energy_stat_ids(data: dict[str, Any]) -> list[str]:
    stats = []
    for source in data.get("water_sources") or []:
        stats += [source.get("stat_energy_from"), source.get("stat_cost")]
    for device in data.get("device_consumption_water") or []:
        stats.append(device.get("stat_consumption"))
    return [s for s in stats if s]


def _increment_gap(prev: dict[str, Any], first: dict[str, Any]) -> float:
    """Consumption between the end of one series and the start of the next."""
    if first.get("last_reset") is not None or prev.get("last_reset") is not None:
        # costs: the sensor restarts at 0, the first hour of the new series
        # is its state since the reset
        return max(min(first.get("state") or 0.0, first.get("sum") or 0.0), 0.0)
    if first.get("state") is None or prev.get("state") is None:
        return 0.0
    # meter readings: difference of the readings (includes the first hour)
    return max(first["state"] - prev["state"], 0.0)


def _to_data(row: dict[str, Any], sum_value: float | None) -> dict[str, Any]:
    data: dict[str, Any] = {"start": dt_util.utc_from_timestamp(row["start"])}
    for key in ("state", "mean", "min", "max"):
        if row.get(key) is not None:
            data[key] = row[key]
    if sum_value is not None:
        data["sum"] = sum_value
    if row.get("last_reset") is not None:
        data["last_reset"] = dt_util.utc_from_timestamp(row["last_reset"])
    return data


async def async_merge(hass: HomeAssistant, plan: MergePlan) -> int:
    """Copy the old statistics into the target. Returns imported hours."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        async_import_statistics,
    )

    ids = {plan.target} | {s.statistic_id for s in plan.sources}
    rows = await _rows(hass, ids)
    meta = await _metadata(hass, ids)
    target_rows = rows.get(plan.target) or []
    target_start = _first_start(target_rows)
    has_sum = bool((meta.get(plan.target) or meta[plan.sources[0].statistic_id])["has_sum"])

    imported: list[dict[str, Any]] = []
    last_row: dict[str, Any] | None = None  # raw row of the previous series
    last_sum = 0.0  # adjusted sum of the last imported row
    cursor = float("-inf")

    for source in plan.sources:
        src = [
            r
            for r in rows.get(source.statistic_id) or []
            if r["start"] > cursor and (target_start is None or r["start"] < target_start)
        ]
        if not src:
            continue
        offset = 0.0
        if has_sum and last_row is not None:
            # continue the running sum of the previous series
            offset = last_sum + _increment_gap(last_row, src[0]) - (src[0].get("sum") or 0.0)
        for row in src:
            new_sum = (row.get("sum") or 0.0) + offset if has_sum else None
            imported.append(_to_data(row, new_sum))
            if has_sum:
                last_sum = new_sum or 0.0
        last_row = src[-1]
        cursor = src[-1]["start"]

    if not imported:
        return 0

    metadata = dict(meta.get(plan.target) or meta[plan.sources[0].statistic_id])
    metadata["statistic_id"] = plan.target
    metadata["source"] = RECORDER
    metadata.pop("has_mean", None)
    instance = get_instance(hass)
    async_import_statistics(hass, metadata, imported)  # type: ignore[arg-type]

    if has_sum and target_rows and last_row is not None:
        first = target_rows[0]
        adjustment = last_sum + _increment_gap(last_row, first) - (first.get("sum") or 0.0)
        instance.async_adjust_statistics(
            plan.target,
            dt_util.utc_from_timestamp(first["start"]),
            adjustment,
            metadata["unit_of_measurement"] or "",
        )
    await instance.async_block_till_done()
    _LOGGER.info(
        "Merged %d hours of %s into %s",
        len(imported),
        ", ".join(s.statistic_id for s in plan.sources),
        plan.target,
    )
    return len(imported)


async def async_execute(
    hass: HomeAssistant, plan: LegacyPlan, delete_old: bool
) -> dict[str, int]:
    """Run the plan."""
    result = {"hours": 0, "series": 0, "removed": 0}
    for merge in plan.merges:
        hours = await async_merge(hass, merge)
        result["hours"] += hours
        result["series"] += 1 if hours else 0

    if plan.energy:
        from homeassistant.components.energy.data import (  # noqa: PLC0415
            async_get_manager,
        )

        manager = await async_get_manager(hass)
        await manager.async_update(_replace_energy(manager.data or {}, plan.energy))

    ent_reg = er.async_get(hass)
    for entity_id in plan.remove_entities:
        if ent_reg.async_get(entity_id):
            ent_reg.async_remove(entity_id)
            result["removed"] += 1
        if (state := hass.states.get(entity_id)) and state.attributes.get("restored"):
            hass.states.async_remove(entity_id)
    dev_reg = dr.async_get(hass)
    for device_id in plan.remove_devices:
        if dev_reg.async_get(device_id) and not er.async_entries_for_device(
            ent_reg, device_id, include_disabled_entities=True
        ):
            dev_reg.async_remove_device(device_id)

    if delete_old and plan.old_statistic_ids:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415

        instance = get_instance(hass)
        instance.async_clear_statistics(plan.old_statistic_ids)
        await instance.async_block_till_done()
        # the old cost sensors only showed the deleted statistics
        for entity_id in plan.cost_entities:
            if ent_reg.async_get(entity_id):
                ent_reg.async_remove(entity_id)
                result["removed"] += 1
            if (state := hass.states.get(entity_id)) and state.attributes.get("restored"):
                hass.states.async_remove(entity_id)
    return result


def _replace_energy(data: dict[str, Any], replace: dict[str, str]) -> dict[str, Any]:
    """Point the energy dashboard to the new statistics (no duplicates)."""
    update: dict[str, Any] = {}

    water = []
    seen: set[str] = set()
    for source in data.get("water_sources") or []:
        new = dict(source)
        new["stat_energy_from"] = replace.get(source["stat_energy_from"], source["stat_energy_from"])
        if new.get("stat_cost"):
            new["stat_cost"] = replace.get(new["stat_cost"], new["stat_cost"])
        if new["stat_energy_from"] in seen:
            continue
        seen.add(new["stat_energy_from"])
        water.append(new)
    update["water_sources"] = water

    devices = []
    seen = set()
    for device in data.get("device_consumption_water") or []:
        new = dict(device)
        new["stat_consumption"] = replace.get(device["stat_consumption"], device["stat_consumption"])
        if new["stat_consumption"] in seen:
            continue
        seen.add(new["stat_consumption"])
        devices.append(new)
    update["device_consumption_water"] = devices
    return update


def format_date(value: datetime | None) -> str:
    return dt_util.as_local(value).strftime("%d.%m.%Y") if value else "-"


def _rows_md(merges: list[MergePlan], with_amount: bool) -> str:
    rows = []
    for merge in merges:
        for i, source in enumerate(merge.sources):
            row = (
                f"| `{source.statistic_id}` | "
                f"{'`' + merge.target + '`' if i == 0 else '〃'} | "
                f"{format_date(source.first)} – {format_date(source.last)} | {source.hours} |"
            )
            if with_amount:
                amount = "-" if source.amount is None else f"{source.amount:.2f}"
                row += f" {amount} {merge.unit or ''} |"
            rows.append(row)
    return "\n".join(rows)


def describe(plan: LegacyPlan) -> dict[str, str]:
    """Markdown for the preview."""
    water = [m for m in plan.merges if not m.is_cost]
    costs = [m for m in plan.merges if m.is_cost]
    energy = "\n".join(f"- `{old}` → `{new}`" for old, new in plan.energy.items())
    remove = ", ".join(f"`{e}`" for e in plan.remove_entities)
    return {
        "table": _rows_md(water, False) or "| - | - | - | - |",
        "costs": _rows_md(costs, True) or "| - | - | - | - | - |",
        "energy": energy or "-",
        "remove": remove or "-",
        "cost_entities": ", ".join(f"`{e}`" for e in plan.cost_entities) or "-",
    }
