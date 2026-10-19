"""Constants for the Xiaomi Smart Pet Feeder integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "xiaomi_pet_feeder"
MANUFACTURER: Final = "Xiaomi"

CONF_SERVER: Final = "server"
CONF_CLIENT_ID: Final = "client_id"
CONF_CREDENTIALS: Final = "credentials"
CONF_DEVICES: Final = "devices"
CONF_LOGIN_METHOD: Final = "login_method"

CONF_STATUS_INTERVAL: Final = "status_interval"
CONF_RECORDS_INTERVAL: Final = "records_interval"
CONF_OFFICIAL_PUSH: Final = "official_push"

DEFAULT_STATUS_INTERVAL: Final = 30
DEFAULT_RECORDS_INTERVAL: Final = 300
DEFAULT_OFFICIAL_PUSH: Final = True
CONSUMABLES_INTERVAL: Final = timedelta(hours=6)

SERVER_NAMES: Final = {
    "cn": "China (cn)",
    "de": "Europe (de)",
    "i2": "India (i2)",
    "ru": "Russia (ru)",
    "sg": "Singapore (sg)",
    "us": "United States (us)",
    "tw": "Taiwan (tw)",
}

ATTR_PORTIONS: Final = "portions"
ATTR_TIME: Final = "time"
ATTR_ENTRIES: Final = "entries"
ATTR_ENABLED: Final = "enabled"
ATTR_AUTO_FEED: Final = "auto_feed"
ATTR_DATE: Final = "date"
ATTR_DAYS: Final = "days"

EVENT_MEAL: Final = "meal"
