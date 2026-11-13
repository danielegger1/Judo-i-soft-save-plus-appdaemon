"""Constants for the JUDO i-soft SAFE+ integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "judo_isoft_save_plus"
MANUFACTURER: Final = "JUDO"
MODEL: Final = "i-soft SAFE+"

# Event fired for every new warning/error from the device's fault memory.
# Replaces the old MQTT topic "<LOCATION>/<NAME>/notify".
EVENT_MESSAGE: Final = f"{DOMAIN}_message"

# Options
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_PERSISTENT_NOTIFICATIONS: Final = "persistent_notifications"
CONF_SODIUM_CHECK: Final = "sodium_check"
CONF_SODIUM_INPUT: Final = "sodium_input"
CONF_SODIUM_LIMIT: Final = "sodium_limit"
CONF_LIMIT_EXTRACTION_TIME: Final = "limit_extraction_time"
CONF_LIMIT_MAX_WATERFLOW: Final = "limit_max_waterflow"
CONF_LIMIT_EXTRACTION_QUANTITY: Final = "limit_extraction_quantity"

DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 3600
DEFAULT_PERSISTENT_NOTIFICATIONS: Final = True
DEFAULT_SODIUM_CHECK: Final = False
DEFAULT_SODIUM_INPUT: Final = 10.0  # mg/L sodium in raw water
DEFAULT_SODIUM_LIMIT: Final = 200.0  # mg/L, German drinking water limit
DEFAULT_LIMIT_EXTRACTION_TIME: Final = 60  # min, device max 600
DEFAULT_LIMIT_MAX_WATERFLOW: Final = 3000  # L/h, device max 5000
DEFAULT_LIMIT_EXTRACTION_QUANTITY: Final = 500  # L, device max 3000

DEVICE_MAX_EXTRACTION_TIME: Final = 600
DEVICE_MAX_WATERFLOW: Final = 5000
DEVICE_MAX_EXTRACTION_QUANTITY: Final = 3000

# 1 °dH of softening adds about 8.2 mg/L sodium (ion exchange)
SODIUM_PER_DH: Final = 8.2

STORAGE_VERSION: Final = 1

# Write register indices (myjudo.eu "write data")
REG_OUTPUT_HARDNESS: Final = 60
REG_START_REGENERATION: Final = 65
REG_WATER_LOCK_CLOSE: Final = 72
REG_WATER_LOCK_OPEN: Final = 73
REG_EXTRACTION_TIME: Final = 74
REG_MAX_WATERFLOW: Final = 75
REG_EXTRACTION_QUANTITY: Final = 76
REG_HOLIDAY_MODE: Final = 77
REG_SALT_STOCK: Final = 94
REG_SLEEPMODE: Final = 171

# Holiday mode: option key <-> raw device value
HOLIDAY_OFF: Final = "off"
HOLIDAY_WATERLOCK: Final = "waterlock"
HOLIDAY_MODE_1: Final = "mode_1"
HOLIDAY_MODE_2: Final = "mode_2"
HOLIDAY_OPTIONS: Final = [HOLIDAY_OFF, HOLIDAY_WATERLOCK, HOLIDAY_MODE_1, HOLIDAY_MODE_2]
HOLIDAY_TO_RAW: Final = {
    HOLIDAY_OFF: 0,
    HOLIDAY_WATERLOCK: 9,
    HOLIDAY_MODE_1: 3,
    HOLIDAY_MODE_2: 5,
}
RAW_TO_HOLIDAY: Final = {raw: key for key, raw in HOLIDAY_TO_RAW.items()}

# Home Assistant rejects states longer than 255 characters
MAX_STATE_LEN: Final = 255


def scan_interval(seconds: int) -> timedelta:
    """Return a clamped polling interval."""
    return timedelta(seconds=max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, int(seconds))))

# Config entry data: pending takeover of the AppDaemon/MQTT bridge history
CONF_LEGACY_MIGRATION: Final = "legacy_migration"
CONF_MIGRATE_HISTORY: Final = "migrate_history"
CONF_LEGACY_GROUP: Final = "legacy_group"
CONF_DELETE_OLD_STATISTICS: Final = "delete_old_statistics"
