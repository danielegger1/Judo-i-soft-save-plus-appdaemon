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
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import JudoApi, JudoAuthError, JudoConnectionError, JudoError
from .const import (
    CONF_DELETE_OLD_STATISTICS,
    CONF_LEGACY_GROUP,
    CONF_LEGACY_MIGRATION,
    CONF_LIMIT_EXTRACTION_QUANTITY,
    CONF_MIGRATE_HISTORY,
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
from .legacy import LegacyGroup, bridge_still_running, find_legacy_groups
from .statistics_merge import LegacyPlan, describe as describe_plan
from .transfer import async_plan as async_transfer_plan, async_run as async_run_transfer

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

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._device_count = 0
        self._groups: list[LegacyGroup] = []
        self._group: LegacyGroup | None = None

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
        self._device_count = len(devices)
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
                self._data = {
                    CONF_USERNAME: username,
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                # History of the old AppDaemon/MQTT bridge can only be
                # assigned unambiguously with a single device
                if self._device_count == 1:
                    self._groups = find_legacy_groups(self.hass)
                if len(self._groups) == 1:
                    self._group = self._groups[0]
                    return await self.async_step_legacy()
                if len(self._groups) > 1:
                    return await self.async_step_legacy_pick()
                return self._create()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, user_input
            ),
            errors=errors,
        )

    def _create(self, extra: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"JUDO i-soft SAFE+ ({self._data[CONF_USERNAME]})",
            data={**self._data, **(extra or {})},
        )

    async def async_step_legacy_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Several old bridges found: choose the one of this device."""
        if user_input is not None:
            if user_input[CONF_LEGACY_GROUP] == "none":
                return self._create()
            self._group = next(
                g for g in self._groups if g.client_id == user_input[CONF_LEGACY_GROUP]
            )
            return await self.async_step_legacy()
        options = [
            SelectOptionDict(
                value=g.client_id,
                label=f"{g.client_id} ({len(g.transferable)})",
            )
            for g in self._groups
        ]
        options.append(SelectOptionDict(value="none", label="-"))
        return self.async_show_form(
            step_id="legacy_pick",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LEGACY_GROUP, default=self._groups[0].client_id
                    ): SelectSelector(SelectSelectorConfig(options=options))
                }
            ),
        )

    async def async_step_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the found entities of the old bridge and ask for the takeover."""
        group = self._group
        assert group is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_MIGRATE_HISTORY]:
                return self._create()
            if bridge_still_running(self.hass, group):
                errors["base"] = "bridge_running"
            else:
                return self._create(
                    {
                        CONF_LEGACY_MIGRATION: {
                            "entities": group.migration(),
                            "remove": [
                                e.entity_id for e in group.entities if not e.transferable
                            ],
                            "device_id": group.device_id,
                        }
                    }
                )

        return self.async_show_form(
            step_id="legacy",
            data_schema=vol.Schema(
                {vol.Required(CONF_MIGRATE_HISTORY, default=True): BooleanSelector()}
            ),
            description_placeholders=await self._legacy_placeholders(group),
            errors=errors,
        )

    async def _legacy_placeholders(self, group: LegacyGroup) -> dict[str, str]:
        """Markdown table old entity -> new entity for the preview."""
        language = self.hass.config.language
        translations = await async_get_translations(
            self.hass, language, "entity", {DOMAIN}
        )
        with_statistics: set[str] = set()
        if "recorder" in self.hass.config.components:
            try:
                from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
                    async_list_statistic_ids,
                )

                ids = {e.entity_id for e in group.transferable}
                with_statistics = {
                    s["statistic_id"]
                    for s in await async_list_statistic_ids(self.hass, ids)
                }
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not list statistics", exc_info=True)

        rows = []
        for ent in group.transferable:
            assert ent.key is not None
            domain = ent.entity_id.split(".", 1)[0]
            name = translations.get(
                f"component.{DOMAIN}.entity.{domain}.{ent.key}.name", ent.key
            )
            state = self.hass.states.get(ent.entity_id)
            value = "-"
            if state is not None:
                unit = state.attributes.get("unit_of_measurement") or ""
                value = f"{state.state} {unit}".strip()
            stats = "✔" if ent.entity_id in with_statistics else ""
            rows.append(f"| `{ent.entity_id}` | {name} | {value} | {stats} |")
        skipped = ", ".join(
            f"`{e.entity_id}`" for e in group.entities if not e.transferable
        )
        return {
            "client_id": group.client_id,
            "count": str(len(rows)),
            "table": "\n".join(rows),
            "skipped": skipped or "-",
        }

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
    """Options: settings and statistics transfer of the old bridge."""

    _plan: LegacyPlan | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "statistics"])

    async def async_step_statistics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Preview and run the statistics transfer of the old bridge."""
        if user_input is not None and self._plan is not None:
            await async_run_transfer(
                self.hass,
                self.config_entry,
                self._plan,
                user_input[CONF_DELETE_OLD_STATISTICS],
            )
            # keep the options unchanged (no reload)
            return self.async_create_entry(data=dict(self.config_entry.options))
        self._plan = await async_transfer_plan(self.hass, self.config_entry)
        if self._plan.empty:
            return self.async_abort(reason="nothing_to_transfer")
        return self.async_show_form(
            step_id="statistics",
            data_schema=vol.Schema(
                {vol.Required(CONF_DELETE_OLD_STATISTICS, default=False): bool}
            ),
            description_placeholders=describe_plan(self._plan),
        )

    async def async_step_settings(
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
        return self.async_show_form(step_id="settings", data_schema=schema)
