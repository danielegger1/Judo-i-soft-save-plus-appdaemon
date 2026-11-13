"""History and statistics really continue after the takeover (real recorder)."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from homeassistant import config_entries
from homeassistant.components.recorder import history, statistics
from homeassistant.components.recorder.util import get_instance
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.judo_isoft_save_plus.const import CONF_MIGRATE_HISTORY, DOMAIN

from .test_legacy import _add_legacy, _old_id

TOTAL = _old_id("Gesamtwasserverbrauch")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder must be set up before hass (overrides conftest)."""
    yield


async def test_history_and_statistics_continue(
    hass: HomeAssistant, mock_api, freezer: FrozenDateTimeFactory
) -> None:
    start = dt_util.parse_datetime("2026-11-09 08:00:00+00:00")
    freezer.move_to(start)
    assert await async_setup_component(hass, "sensor", {})
    _add_legacy(hass)
    # old bridge: a few hours of values with long-term statistics
    attrs = {
        "unit_of_measurement": "m³",
        "device_class": "water",
        "state_class": "total_increasing",
    }
    for i, value in enumerate(("123.0", "123.2", "123.4")):
        freezer.move_to(start + timedelta(minutes=30 * i))
        hass.states.async_set(TOTAL, value, attrs)
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(hours=1, minutes=1))
    do_adhoc_statistics(hass, start=start + timedelta(minutes=55))  # 08:00-09:00
    await async_wait_recording_done(hass)
    old_stats = await get_instance(hass).async_add_executor_job(
        statistics.list_statistic_ids, hass, {TOTAL}
    )
    assert [s["statistic_id"] for s in old_stats] == [TOTAL]

    freezer.move_to(start + timedelta(hours=2))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "user", CONF_PASSWORD: "pw"}
    )
    assert result["step_id"] == "legacy"
    assert f"| `{TOTAL}` |" in result["description_placeholders"]["table"]
    # statistics column
    row = next(
        r for r in result["description_placeholders"]["table"].splitlines() if TOTAL in r
    )
    assert row.endswith("✔ |")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MIGRATE_HISTORY: True}
    )
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(TOTAL).platform == DOMAIN
    assert hass.states.get(TOTAL).state == "123.456"
    await async_wait_recording_done(hass)

    # history: old values of the bridge and new value of the integration
    states = await get_instance(hass).async_add_executor_job(
        history.state_changes_during_period,
        hass,
        start - timedelta(minutes=1),
        dt_util.utcnow() + timedelta(minutes=1),
        TOTAL,
    )
    values = [s.state for s in states[TOTAL]]
    assert {"123.0", "123.2", "123.4", "123.456"} <= set(values), values

    # long-term statistics are continued under the same statistic_id
    freezer.move_to(start + timedelta(hours=3, minutes=1))
    do_adhoc_statistics(hass, start=start + timedelta(hours=2, minutes=55))  # 10-11
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics.statistics_during_period,
        hass,
        start - timedelta(hours=1),
        None,
        {TOTAL},
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats[TOTAL]
    assert rows[0]["state"] == pytest.approx(123.2)  # 08:00-09:00
    assert rows[-1]["state"] == pytest.approx(123.456)
    # no reset: sum grows by exactly the consumption
    assert rows[-1]["sum"] - rows[0]["sum"] == pytest.approx(0.256)
