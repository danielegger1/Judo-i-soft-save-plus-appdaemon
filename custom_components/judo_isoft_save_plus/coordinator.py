"""Data update coordinator for JUDO i-soft SAFE+."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import JudoApi, JudoAuthError, JudoDevice, JudoError
from .const import (
    CONF_PERSISTENT_NOTIFICATIONS,
    CONF_SCAN_INTERVAL,
    DEFAULT_PERSISTENT_NOTIFICATIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_MESSAGE,
    MAX_STATE_LEN,
    RAW_TO_HOLIDAY,
    HOLIDAY_OFF,
    STORAGE_VERSION,
    scan_interval,
)
from .messages import message_text

if TYPE_CHECKING:
    from . import JudoConfigEntry

_LOGGER = logging.getLogger(__name__)

# Max. number of fault memory entries reported in one poll
MAX_NEW_MESSAGES = 10


@dataclass
class JudoState:
    """Parsed and derived values of one device."""

    device: JudoDevice
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class JudoData:
    """Coordinator data."""

    devices: dict[str, JudoState]
    last_message: dict[str, Any] | None = None


def _scaled(value: int | None, divisor: float, digits: int = 3) -> float | None:
    return None if value is None else round(value / divisor, digits)


def parse_values(device: JudoDevice) -> dict[str, Any]:
    """Convert the raw registers into values (None = not available)."""
    reg = device.register
    values: dict[str, Any] = {}

    hours = reg(7, 0, 4)
    values["next_revision"] = None if hours is None else hours // 24

    total_l = reg(8, 0, 8)
    soft_l = reg(9, 0, 8)
    values["total_water_l"] = total_l
    values["total_softwater"] = _scaled(soft_l, 1000)
    values["total_hardwater"] = (
        None
        if total_l is None or soft_l is None
        else round(max(total_l - soft_l, 0) / 1000, 3)
    )

    values["salt_stock"] = _scaled(reg(94, 0, 4), 1000, 1)
    values["salt_range"] = reg(94, 4, 8)
    values["output_hardness"] = reg(790, 18, 20)
    values["input_hardness"] = reg(790, 54, 56)
    values["water_flow"] = reg(790, 34, 38)
    values["regenerations"] = reg(791, 62, 66)

    regen = reg(791, 2, 4)
    values["regeneration_running"] = None if regen is None else bool(regen & 0x0F)

    values["battery"] = reg(93, 6, 8)

    lock = reg(792, 2, 4)
    values["water_lock"] = None if lock is None else lock != 0

    values["sleepmode"] = reg(792, 20, 22)
    values["max_waterflow"] = reg(792, 26, 30)
    values["extraction_quantity"] = reg(792, 30, 34)
    values["extraction_time"] = reg(792, 34, 38)

    holiday = reg(792, 38, 40)
    values["holiday_mode"] = (
        None if holiday is None else RAW_TO_HOLIDAY.get(holiday, HOLIDAY_OFF)
    )
    return values


class JudoCoordinator(DataUpdateCoordinator[JudoData]):
    """Polls myjudo.eu."""

    config_entry: JudoConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: JudoConfigEntry, api: JudoApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=scan_interval(
                entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.api = api
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._stored: dict[str, Any] = {}

    async def async_load_storage(self) -> None:
        """Load persisted counters (replaces temp_getjudo.pkl)."""
        self._stored = await self._store.async_load() or {}
        self._stored.setdefault("devices", {})

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self._stored, 10)

    async def _async_update_data(self) -> JudoData:
        try:
            devices = await self.api.get_devices()
        except JudoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except JudoError as err:
            raise UpdateFailed(f"myjudo.eu: {err}") from err

        if not devices:
            raise UpdateFailed("myjudo.eu returned no device")

        states: dict[str, JudoState] = {}
        for serial, device in devices.items():
            values = parse_values(device)
            self._derive(serial, values)
            states[serial] = JudoState(device=device, values=values)

        # The fault memory is optional: a failure there must not make all
        # entities unavailable.
        last_message = self.data.last_message if self.data else None
        try:
            entries = await self.api.get_error_messages()
        except JudoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except JudoError as err:
            _LOGGER.debug("Could not read fault memory: %s", err)
        else:
            last_message = self._process_messages(entries) or last_message

        if last_message is None:
            last_message = self._stored.get("last_message")

        self._save()
        return JudoData(devices=states, last_message=last_message)

    def _derive(self, serial: str, values: dict[str, Any]) -> None:
        """Calculate consumption and regeneration statistics."""
        store = self._stored["devices"].setdefault(serial, {})
        now = time.time()

        # --- total water, never decreasing ---------------------------------
        # A decreasing value would be counted as meter reset by the
        # long-term statistics (total_increasing).
        total_l = values.pop("total_water_l")
        prev_total = store.get("last_total_l")
        if total_l is not None and prev_total is not None and total_l < prev_total:
            _LOGGER.debug(
                "Ignoring decreasing total water %s L (last %s L)", total_l, prev_total
            )
            total_l = prev_total
        if total_l is not None:
            store["last_total_l"] = total_l
        values["total_water"] = None if total_l is None else round(total_l / 1000, 3)

        # --- consumption today / yesterday -----------------------------------
        # Uses the full date. The old script compared only the day of the
        # month and lost everything on a restart outside AppDaemon.
        today = dt_util.now().date().isoformat()
        if total_l is not None:
            if store.get("day") != today:
                old_day = store.get("day")
                if old_day is not None and prev_total is not None:
                    store["yesterday_l"] = (
                        max(prev_total - store.get("offset_total_l", prev_total), 0)
                        if _is_yesterday(old_day, today)
                        else None
                    )
                store["day"] = today
                store["offset_total_l"] = prev_total if prev_total is not None else total_l
            values["water_today"] = max(total_l - store["offset_total_l"], 0)
        else:
            values["water_today"] = None
        values["water_yesterday"] = store.get("yesterday_l")

        # --- regenerations ---------------------------------------------------
        count = values.get("regenerations")
        if count is not None and count > 0:
            last_val = store.get("reg_last_val")
            last_ts = store.get("reg_last_ts")
            if last_val is None or count < last_val:
                # first start or counter reset: only synchronise
                store["reg_last_val"] = count
                store["reg_last_ts"] = now
                store["reg_ts_exact"] = False
            elif count - last_val == 1:
                if store.get("reg_ts_exact") and last_ts:
                    interval_h = (now - last_ts) / 3600
                    n = store.get("reg_mean_count", 0)
                    mean = store.get("reg_mean_h", 0.0)
                    store["reg_mean_h"] = round((mean * n + interval_h) / (n + 1), 1)
                    store["reg_mean_count"] = n + 1
                store["reg_last_val"] = count
                store["reg_last_ts"] = now
                store["reg_ts_exact"] = True
            elif count > last_val:
                # several regenerations missed (offline): time is known,
                # but the interval is not
                store["reg_last_val"] = count
                store["reg_last_ts"] = now
                store["reg_ts_exact"] = True

        values["avg_regeneration_interval"] = (
            store.get("reg_mean_h") if store.get("reg_mean_count") else None
        )
        # Only known once a regeneration was actually observed. The old
        # script reported "0 h" right after every first start.
        values["hours_since_regeneration"] = (
            int((now - store["reg_last_ts"]) / 3600)
            if store.get("reg_ts_exact") and store.get("reg_last_ts")
            else None
        )

    def _process_messages(self, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Report new fault memory entries, return the newest one."""
        first_run = "last_err_id" not in self._stored
        if not entries:
            # empty fault memory: everything that shows up later is new
            self._stored.setdefault("last_err_id", None)
            return None

        last_id = self._stored.get("last_err_id")

        new: list[dict[str, Any]] = []
        for entry in entries[:MAX_NEW_MESSAGES]:
            if str(entry.get("id")) == str(last_id):
                break
            new.append(entry)
        if not new:
            return None

        self._stored["last_err_id"] = entries[0].get("id")
        language = self.hass.config.language
        newest: dict[str, Any] | None = None

        # oldest first, so automations see them in the right order
        for entry in reversed(new):
            msg_type = str(entry.get("type", "")).lower()
            code = str(entry.get("error", ""))
            text = message_text(language, msg_type, code)
            message = {
                "message": text[:MAX_STATE_LEN],
                "type": msg_type,
                "code": code,
                "timestamp": _format_ts(entry.get("ts_sort")),
                "id": entry.get("id"),
            }
            newest = message

            # On the very first run the fault memory may contain old
            # entries; show the newest one but do not alert.
            if first_run:
                continue

            _LOGGER.info("JUDO message: %s", text)
            self.hass.bus.async_fire(EVENT_MESSAGE, message)
            if self.config_entry.options.get(
                CONF_PERSISTENT_NOTIFICATIONS, DEFAULT_PERSISTENT_NOTIFICATIONS
            ):
                title = "JUDO i-soft SAFE+"
                persistent_notification.async_create(
                    self.hass,
                    f"{message['timestamp']} {text}".strip(),
                    title=title,
                    notification_id=f"{DOMAIN}_{entry.get('id')}",
                )

        if newest is not None:
            self._stored["last_message"] = newest
        return newest

    async def async_write(self, serial: str, index: int, value: str = "") -> None:
        """Write a register and raise a HA error on failure."""
        state = self.data.devices.get(serial) if self.data else None
        if state is None:
            raise HomeAssistantError(f"Device {serial} is not available")
        try:
            await self.api.write(state.device, index, value)
        except JudoAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"myjudo.eu login failed: {err}") from err
        except JudoError as err:
            raise HomeAssistantError(f"Command failed: {err}") from err

    def set_value(self, serial: str, key: str, value: Any) -> None:
        """Optimistically update a value after a successful write."""
        if self.data and serial in self.data.devices:
            self.data.devices[serial].values[key] = value
            self.async_update_listeners()


def _is_yesterday(day: str, today: str) -> bool:
    try:
        return (
            datetime.fromisoformat(today).date() - datetime.fromisoformat(day).date()
        ).days == 1
    except ValueError:
        return False


def _format_ts(raw: Any) -> str:
    """Format the "ts_sort" field like the old script did."""
    if raw is None:
        return ""
    text = str(raw)
    return text[:-7] if len(text) >= 7 else text
