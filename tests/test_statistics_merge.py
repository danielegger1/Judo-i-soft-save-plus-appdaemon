"""Statistics transfer after setup (the user's real situation)."""

from __future__ import annotations

from datetime import datetime, timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.judo_isoft_save_plus.const import (
    CONF_DELETE_OLD_STATISTICS,
    DOMAIN,
)
from custom_components.judo_isoft_save_plus.statistics_merge import _replace_energy

from .conftest import SERIAL

OLD_EN = "sensor.isoftsafeplus_home_total_water_consumption"
OLD_DE = "sensor.isoftsafeplus_home_gesamtwasserverbrauch"
OLD_DE_COST = OLD_DE + "_cost"
OLD_SOFT = "sensor.isoftsafeplus_home_gesamtweichwasserverbrauch"
OLD_SWITCH = "switch.isoftsafeplus_home_water_lock"
T0 = datetime(2026, 11, 1, tzinfo=dt_util.UTC)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder must be set up before hass."""
    yield


def _meta(statistic_id: str, unit: str, unit_class: str | None) -> dict:
    return {
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
        "unit_class": unit_class,
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": None,
    }


def _import(hass, statistic_id, unit, unit_class, hours, states, sums, reset=None):
    async_import_statistics(
        hass,
        _meta(statistic_id, unit, unit_class),
        [
            {
                "start": T0 + timedelta(hours=h),
                "state": st,
                "sum": su,
                **({"last_reset": T0 + timedelta(hours=reset)} if reset is not None else {}),
            }
            for h, st, su in zip(hours, states, sums, strict=True)
        ],
    )


async def _stats(hass, statistic_id):
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        T0 - timedelta(days=1),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return [(r["state"], round(r["sum"], 6)) for r in stats.get(statistic_id, [])]


async def test_merge_like_user_installation(
    hass: HomeAssistant, mock_api, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(T0 + timedelta(days=2))
    # integration already set up (new entity ids)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user", data={CONF_USERNAME: "user", CONF_PASSWORD: "pw"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    ent_reg = er.async_get(hass)
    new_total = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_total_water")
    new_soft = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_total_softwater")
    new_cost = new_total + "_cost"

    # old bridge: first with English, later with German names, costs of the
    # energy dashboard, and one left over MQTT switch
    _import(hass, OLD_EN, "m³", "volume", [0, 1, 2], [100.0, 100.1, 100.2], [0.1, 0.2, 0.3])
    _import(hass, OLD_DE, "m³", "volume", [3, 4, 5], [100.4, 100.5, 100.6], [0.1, 0.2, 0.3])
    _import(hass, OLD_SOFT, "m³", "volume", [3, 4], [80.0, 80.1], [0.0, 0.1])
    _import(hass, OLD_DE_COST, "EUR", None, [3, 4, 5], [0.2, 0.4, 0.6], [0.2, 0.4, 0.6], 3)
    # new integration since hour 7
    _import(hass, new_total, "m³", "volume", [7, 8], [101.0, 101.1], [0.05, 0.15])
    _import(hass, new_cost, "EUR", None, [7, 8], [0.1, 0.3], [0.1, 0.3], 7)
    # unavailable cost sensor of the energy dashboard left from the bridge
    ent_reg.async_get_or_create(
        "sensor", "energy", "old_cost", suggested_object_id=OLD_DE_COST.split(".")[1]
    )
    hass.states.async_set(OLD_DE_COST, "unavailable", {"restored": True})
    ent_reg.async_get_or_create(
        "switch", "mqtt", "iSoftSafePlus-Home_Water_lock",
        suggested_object_id="isoftsafeplus_home_water_lock",
    )
    hass.states.async_set(OLD_SWITCH, "unavailable", {"restored": True})
    await async_wait_recording_done(hass)

    # repair issue is created after a reload
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_statistics_{entry.entry_id}")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "statistics"}
    )
    assert result["type"] is FlowResultType.FORM
    table = result["description_placeholders"]["table"]
    for old in (OLD_EN, OLD_DE, OLD_SOFT):
        assert f"`{old}`" in table
    assert f"`{new_total}`" in table
    assert f"`{new_cost}`" in result["description_placeholders"]["costs"]
    placeholders = result["description_placeholders"]
    assert OLD_SWITCH in placeholders["remove"]
    # costs are listed as transferred (with amount), not removed
    assert f"`{OLD_DE_COST}`" in placeholders["costs"]
    assert "0.60 EUR" in placeholders["costs"]
    assert f"`{OLD_DE_COST}`" not in table
    assert OLD_DE_COST not in placeholders["remove"]
    assert OLD_DE_COST in placeholders["cost_entities"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DELETE_OLD_STATISTICS: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    # one continuous series: sum == reading - first reading (99.9)
    assert await _stats(hass, new_total) == [
        (100.0, 0.1), (100.1, 0.2), (100.2, 0.3),
        (100.4, 0.5), (100.5, 0.6), (100.6, 0.7),
        (101.0, 1.1), (101.1, 1.2),
    ]
    # costs continue: 0.6 EUR of the old series + new costs
    assert await _stats(hass, new_cost) == [
        (0.2, 0.2), (0.4, 0.4), (0.6, 0.6), (0.1, 0.7), (0.3, 0.9),
    ]
    # renamed old soft water statistic is merged as well
    assert await _stats(hass, new_soft) == [(80.0, 0.0), (80.1, 0.1)]
    # old statistics and old cost sensor are kept, left over entity is gone,
    # issue is fixed
    assert await _stats(hass, OLD_DE) != []
    assert await _stats(hass, OLD_DE_COST) != []
    assert ent_reg.async_get(OLD_DE_COST) is not None
    assert ent_reg.async_get(OLD_SWITCH) is None
    assert hass.states.get(OLD_SWITCH) is None
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_statistics_{entry.entry_id}")

    # after a restart the issue does not come back (only kept old statistics)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_statistics_{entry.entry_id}")

    # running it again finds nothing new to copy (no duplicates)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "statistics"}
    )
    assert result["type"] is FlowResultType.FORM
    assert "| 0 |" in result["description_placeholders"]["table"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DELETE_OLD_STATISTICS: True}
    )
    await async_wait_recording_done(hass)
    # only now (opt-in) old statistics and old cost sensor are deleted,
    # the transferred costs stay
    assert await _stats(hass, OLD_DE) == []
    assert await _stats(hass, OLD_DE_COST) == []
    assert ent_reg.async_get(OLD_DE_COST) is None
    assert len(await _stats(hass, new_total)) == 8
    assert (await _stats(hass, new_cost))[-1] == (0.3, 0.9)


def test_replace_energy() -> None:
    data = {
        "water_sources": [
            {"type": "water", "stat_energy_from": OLD_DE, "stat_cost": None,
             "entity_energy_price": None, "number_energy_price": 2.5},
            {"type": "water", "stat_energy_from": "sensor.new", "stat_cost": None,
             "entity_energy_price": None, "number_energy_price": 2.5},
            {"type": "water", "stat_energy_from": "sensor.other", "stat_cost": None,
             "entity_energy_price": None, "number_energy_price": None},
        ],
        "device_consumption_water": [{"stat_consumption": OLD_SOFT}],
    }
    update = _replace_energy(data, {OLD_DE: "sensor.new", OLD_SOFT: "sensor.soft"})
    assert [s["stat_energy_from"] for s in update["water_sources"]] == [
        "sensor.new", "sensor.other",
    ]
    assert update["device_consumption_water"] == [{"stat_consumption": "sensor.soft"}]


OLD_EN_COST = OLD_EN + "_cost"


async def test_first_setup_transfers_old_values_first(
    hass: HomeAssistant, mock_api, freezer: FrozenDateTimeFactory
) -> None:
    """MQTT entities already deleted, only statistics left (EN and DE names)."""
    from homeassistant import config_entries

    freezer.move_to(T0 + timedelta(days=2))
    _import(hass, OLD_EN, "m³", "volume", [0, 1, 2], [100.0, 100.1, 100.2], [0.1, 0.2, 0.3])
    _import(hass, OLD_DE, "m³", "volume", [3, 4, 5], [100.4, 100.5, 100.6], [0.1, 0.2, 0.3])
    _import(hass, OLD_SOFT, "m³", "volume", [3, 4], [80.0, 80.1], [0.0, 0.1])
    _import(hass, OLD_EN_COST, "EUR", None, [0, 1, 2], [0.1, 0.2, 0.3], [0.1, 0.2, 0.3], 0)
    _import(hass, OLD_DE_COST, "EUR", None, [3, 4, 5], [0.2, 0.4, 0.6], [0.2, 0.4, 0.6], 3)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "switch", "mqtt", "iSoftSafePlus-Home_Water_lock",
        suggested_object_id="isoftsafeplus_home_water_lock",
    )
    await async_wait_recording_done(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user", CONF_PASSWORD: "pw"}
    )
    assert result["step_id"] == "legacy"
    ph = result["description_placeholders"]
    # newest old statistic takes over its entity_id
    assert f"`{OLD_DE}`" in ph["table"] and f"`{OLD_SOFT}`" in ph["table"]
    assert f"`{OLD_EN}`" not in ph["table"]
    # older English series is merged into it, costs as well (with amount)
    assert f"| `{OLD_EN}` | `{OLD_DE}` |" in ph["statistics"]
    assert f"| `{OLD_EN_COST}` | `{OLD_DE_COST}` |" in ph["costs"]
    assert "0.30 EUR" in ph["costs"]

    from custom_components.judo_isoft_save_plus.const import CONF_MIGRATE_HISTORY

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MIGRATE_HISTORY: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    assert ent_reg.async_get(OLD_DE).platform == DOMAIN
    assert ent_reg.async_get(OLD_SOFT).platform == DOMAIN
    assert ent_reg.async_get("switch.isoftsafeplus_home_water_lock") is None
    # one continuous series under the taken over entity_id, old values first
    assert (await _stats(hass, OLD_DE))[:6] == [
        (100.0, 0.1), (100.1, 0.2), (100.2, 0.3),
        (100.4, 0.5), (100.5, 0.6), (100.6, 0.7),
    ]
    # costs: 0.30 EUR (EN) + 0.60 EUR (DE)
    assert await _stats(hass, OLD_DE_COST) == [
        (0.1, 0.1), (0.2, 0.2), (0.3, 0.3), (0.2, 0.5), (0.4, 0.7), (0.6, 0.9),
    ]
    # old statistics are kept, nothing left to repair
    assert await _stats(hass, OLD_EN_COST) != []
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert not ir.async_get(hass).async_get_issue(DOMAIN, f"legacy_statistics_{entry.entry_id}")
