"""Config flow for JUDO i-soft SAFE+."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import JudoApi, JudoAuthError, JudoConnectionError, JudoError
from .const import (
    CONF_LIMIT_EXTRACTION_QUANTITY,
    CONF_LIMIT_EXTRACTION_TIME,
    CONF_LIMIT_MAX_WATERFLOW,
    CONF_PERSISTENT_NOTIFICATIONS,
    CONF_SCAN_INTERVAL,
    CONF_SODIUM_CHECK,
    CONF_SODIUM_INPUT,
    CONF_SODIUM_LIMIT,
    DEFAULT_LIMIT_EXTRACTION_QUANTITY,
    DEFAULT_LIMIT_EXTRACTION_TIME,
    DEFAULT_LIMIT_MAX_WATERFLOW,
    DEFAULT_PERSISTENT_NOTIFICATIONS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SODIUM_CHECK,
    DEFAULT_SODIUM_INPUT,
    DEFAULT_SODIUM_LIMIT,
    DEVICE_MAX_EXTRACTION_QUANTITY,
    DEVICE_MAX_EXTRACTION_TIME,
    DEVICE_MAX_WATERFLOW,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


class JudoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def _validate(self, username: str, password: str) -> dict[str, str]:
        api = JudoApi(async_get_clientsession(self.hass), username, password)
        try:
            await api.login()
            devices = await api.get_devices()
        except JudoAuthError:
            return {"base": "invalid_auth"}
        except JudoConnectionError:
            return {"base": "cannot_connect"}
        except JudoError:
            _LOGGER.exception("Unexpected answer from myjudo.eu")
            return {"base": "unknown"}
        if not devices:
            return {"base": "no_devices"}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the myjudo.eu credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            errors = await self._validate(username, user_input[CONF_PASSWORD])
            if not errors:
                return self.async_create_entry(
                    title=f"JUDO i-soft SAFE+ ({username})",
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Password changed or token rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._validate(
                entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return JudoOptionsFlow()


def _number(minimum: float, maximum: float, step: float, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=NumberSelectorMode.BOX,
        )
    )


class JudoOptionsFlow(OptionsFlow):
    """Options: polling interval, notifications, sodium check, slider limits."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): _number(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL, 1, "s"),
                vol.Required(
                    CONF_PERSISTENT_NOTIFICATIONS,
                    default=opts.get(
                        CONF_PERSISTENT_NOTIFICATIONS, DEFAULT_PERSISTENT_NOTIFICATIONS
                    ),
                ): bool,
                vol.Required(
                    CONF_SODIUM_CHECK,
                    default=opts.get(CONF_SODIUM_CHECK, DEFAULT_SODIUM_CHECK),
                ): bool,
                vol.Required(
                    CONF_SODIUM_INPUT,
                    default=opts.get(CONF_SODIUM_INPUT, DEFAULT_SODIUM_INPUT),
                ): _number(0, 200, 0.1, "mg/L"),
                vol.Required(
                    CONF_SODIUM_LIMIT,
                    default=opts.get(CONF_SODIUM_LIMIT, DEFAULT_SODIUM_LIMIT),
                ): _number(1, 500, 0.1, "mg/L"),
                vol.Required(
                    CONF_LIMIT_EXTRACTION_TIME,
                    default=opts.get(
                        CONF_LIMIT_EXTRACTION_TIME, DEFAULT_LIMIT_EXTRACTION_TIME
                    ),
                ): _number(10, DEVICE_MAX_EXTRACTION_TIME, 10, "min"),
                vol.Required(
                    CONF_LIMIT_MAX_WATERFLOW,
                    default=opts.get(
                        CONF_LIMIT_MAX_WATERFLOW, DEFAULT_LIMIT_MAX_WATERFLOW
                    ),
                ): _number(500, DEVICE_MAX_WATERFLOW, 500, "L/h"),
                vol.Required(
                    CONF_LIMIT_EXTRACTION_QUANTITY,
                    default=opts.get(
                        CONF_LIMIT_EXTRACTION_QUANTITY,
                        DEFAULT_LIMIT_EXTRACTION_QUANTITY,
                    ),
                ): _number(100, DEVICE_MAX_EXTRACTION_QUANTITY, 100, "L"),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
