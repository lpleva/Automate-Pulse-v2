"""Config flow for Automate Pulse Hub v2 integration."""

import asyncio
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_POLL_INTERVAL,
    CONF_SIGNAL_POLL_INTERVAL,
    CONNECT_TEST_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SIGNAL_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MAX_SIGNAL_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_SIGNAL_POLL_INTERVAL,
)
from .vendor import aiopulse2

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema({vol.Required("host"): str})


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Automate Pulse Hub v2."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        """Handle the initial step once we have info from the user."""
        errors = {}
        if user_input is not None:
            host = user_input["host"].strip()
            try:
                hub = aiopulse2.Hub(host)
                async with asyncio.timeout(CONNECT_TEST_TIMEOUT):
                    await hub.test()
            except (
                OSError,
                asyncio.TimeoutError,
                aiopulse2.HubBaseException,
            ) as err:
                _LOGGER.debug("Cannot connect to hub at %s: %s", host, err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # pylint: disable=broad-except
                # websockets raises its own hierarchy for TLS and handshake
                # failures; those are connection problems too. Anything else
                # is a bug worth seeing in the log.
                if "websockets" in type(err).__module__:
                    _LOGGER.debug("Cannot connect to hub at %s: %s", host, err)
                    errors["base"] = "cannot_connect"
                else:
                    _LOGGER.exception("Unexpected error testing hub at %s", host)
                    errors["base"] = "unknown"
            else:
                if hub.id:
                    await self.async_set_unique_id(hub.id)
                    self._abort_if_unique_id_configured(updates={"host": host})
                title = hub.name or f"Pulse Hub ({host})"
                return self.async_create_entry(title=title, data={"host": host})

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options: how often the hub is polled."""

    async def async_step_init(self, user_input=None):
        """Show and save the polling options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)),
                vol.Required(
                    CONF_SIGNAL_POLL_INTERVAL,
                    default=options.get(
                        CONF_SIGNAL_POLL_INTERVAL, DEFAULT_SIGNAL_POLL_INTERVAL
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SIGNAL_POLL_INTERVAL, max=MAX_SIGNAL_POLL_INTERVAL),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
