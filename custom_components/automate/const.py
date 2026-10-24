"""Constants for the Automate Pulse Hub v2 integration."""

DOMAIN = "automate"

AUTOMATE_HUB_UPDATE = "automate_hub_update_{}"
AUTOMATE_ENTITY_REMOVE = "automate_entity_remove_{}"

# Options (config entry `options`), all in seconds.
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL = 10
MIN_POLL_INTERVAL = 2
MAX_POLL_INTERVAL = 300

CONF_SIGNAL_POLL_INTERVAL = "signal_poll_interval"
DEFAULT_SIGNAL_POLL_INTERVAL = 0  # off until the user opts in (Lukas, 2026-10-08)
MIN_SIGNAL_POLL_INTERVAL = 10  # the library's floor; 0 (off) is accepted separately
MAX_SIGNAL_POLL_INTERVAL = 3600

# Config entry `data` key: the last entry title this integration wrote itself
# (`<hub name> (<host>)`). While the entry's title still equals it, the title is
# ours to refresh; once it differs, a person renamed the entry and it stays.
CONF_AUTO_TITLE = "auto_title"

# Seconds the config flow waits for the hub's first reply before giving up.
CONNECT_TEST_TIMEOUT = 20

# The `automate.refresh_signal` action: one live signal query per targeted shade.
# The library spaces the serial sessions (aiopulse2 SERIAL_SESSION_SPACING).
SERVICE_REFRESH_SIGNAL = "refresh_signal"
