"""Config flow tests."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.judo_isoft_save_plus.api import JudoAuthError, JudoConnectionError
from custom_components.judo_isoft_save_plus.const import CONF_SCAN_INTERVAL, DOMAIN


async def test_user_flow(hass: HomeAssistant, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    mock_api["login"].side_effect = JudoAuthError
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "User", CONF_PASSWORD: "x"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_api["login"].side_effect = JudoConnectionError
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "User", CONF_PASSWORD: "x"}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    mock_api["login"].side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: " User ", CONF_PASSWORD: "pw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_USERNAME: "User", CONF_PASSWORD: "pw"}
    assert result["result"].unique_id == "user"
    await hass.async_block_till_done()


async def test_duplicate(hass: HomeAssistant, mock_api) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="user").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "USER", CONF_PASSWORD: "pw"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user", data={CONF_USERNAME: "user", CONF_PASSWORD: "old"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"
    await hass.async_block_till_done()


async def test_options(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user", data={CONF_USERNAME: "user", CONF_PASSWORD: "pw"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.update_interval.total_seconds() == 60
