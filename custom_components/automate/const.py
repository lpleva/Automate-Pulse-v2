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

# Seconds the config flow waits for the hub's first reply before giving up.
CONNECT_TEST_TIMEOUT = 20

# The `automate.refresh_signal` action: one live signal query per targeted shade,
# one after another, this many seconds apart so the hub's radio is not flooded.
SERVICE_REFRESH_SIGNAL = "refresh_signal"
REFRESH_SIGNAL_SPACING = 0.75
