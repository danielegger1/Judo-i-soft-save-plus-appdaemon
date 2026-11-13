"""Repair flow: take over statistics of the old AppDaemon/MQTT bridge."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_DELETE_OLD_STATISTICS
from .statistics_merge import LegacyPlan, describe
from .transfer import async_plan, async_run


class LegacyStatisticsRepairFlow(RepairsFlow):
    """Preview and run the transfer."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._plan: LegacyPlan | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="nothing_to_transfer")
        if user_input is not None and self._plan is not None:
            await async_run(
                self.hass, entry, self._plan, user_input[CONF_DELETE_OLD_STATISTICS]
            )
            return self.async_create_entry(data={})
        self._plan = await async_plan(self.hass, entry)
        if self._plan.empty:
            return self.async_abort(reason="nothing_to_transfer")
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_DELETE_OLD_STATISTICS, default=False): bool}
            ),
            description_placeholders=describe(self._plan),
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the repair flow."""
    assert data is not None
    return LegacyStatisticsRepairFlow(data["entry_id"])
