"""Config flow for the Tantos Jolly integration.

Each config entry represents one channel (one call panel or one camera) on
the hub -- add the integration again to add another channel from the same
or a different hub.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import CHANNEL_CHOICES, CONF_CHANNEL, CONF_RTSP_PORT, DEFAULT_RTSP_BASE_PORT, DOMAIN
from .glnk_client import GlnkSession

_LOGGER = logging.getLogger(__name__)


def _test_login(host: str, username: str, password: str, channel: int) -> tuple[bool, str | None]:
    """Blocking: runs the real handshake+login against the device. Returns
    (success, error_reason)."""
    sess = GlnkSession(host, timeout=5.0)
    try:
        if not sess.handshake():
            return False, (
                "handshake_failed -- device unreachable, or the vhOme app on a "
                "phone currently holds the only session slot (close it and retry)"
            )
        resp = sess.login(username, password, channel=channel)
        if not resp:
            return False, "login_failed -- check username/password"
        return True, None
    except OSError as err:
        return False, f"network_error: {err}"
    finally:
        sess.close()


class TantosJollyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tantos Jolly."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            channel = int(user_input[CONF_CHANNEL])

            unique_id = f"{host}-{channel:#x}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            ok, reason = await self.hass.async_add_executor_job(
                _test_login, host, username, password, channel
            )
            if not ok:
                _LOGGER.debug("Tantos Jolly setup test failed: %s", reason)
                errors["base"] = "cannot_connect"
            else:
                data = {
                    CONF_HOST: host,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                    CONF_CHANNEL: channel,
                    CONF_RTSP_PORT: DEFAULT_RTSP_BASE_PORT + channel,
                }
                title = f"{CHANNEL_CHOICES[channel]} ({host})"
                return self.async_create_entry(title=title, data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME, default="admin"): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_CHANNEL): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=str(value), label=label)
                            for value, label in CHANNEL_CHOICES.items()
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
