"""Run the statistics transfer and report it."""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .statistics_merge import LegacyPlan, async_build_plan, async_execute

ISSUE_ID = "legacy_statistics"


def entry_serial(entry: ConfigEntry) -> str | None:
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None or coordinator.data is None or len(coordinator.data.devices) != 1:
        return None
    return next(iter(coordinator.data.devices))


async def async_plan(hass: HomeAssistant, entry: ConfigEntry) -> LegacyPlan:
    if (serial := entry_serial(entry)) is None:
        return LegacyPlan()
    return await async_build_plan(hass, serial)


async def async_check_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Show a repair if data of the old bridge is left."""
    plan = await async_plan(hass, entry)
    if plan.merges or plan.remove_entities:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_ID}_{entry.entry_id}",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_ID,
            data={"entry_id": entry.entry_id},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_ID}_{entry.entry_id}")


async def async_run(
    hass: HomeAssistant, entry: ConfigEntry, plan: LegacyPlan, delete_old: bool
) -> None:
    result = await async_execute(hass, plan, delete_old)
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_ID}_{entry.entry_id}")
    if hass.config.language.startswith("de"):
        text = (
            f"{result['hours']} Stunden Langzeitstatistik aus {result['series']} "
            f"Datenreihen übernommen, {len(plan.energy)} Einträge im Energie-Dashboard "
            f"umgestellt, {result['removed']} alte Entitäten entfernt."
        )
    else:
        text = (
            f"Took over {result['hours']} hours of long-term statistics from "
            f"{result['series']} series, switched {len(plan.energy)} energy dashboard "
            f"entries, removed {result['removed']} old entities."
        )
    persistent_notification.async_create(
        hass, text, title="JUDO i-soft SAFE+", notification_id=f"{DOMAIN}_statistics"
    )
