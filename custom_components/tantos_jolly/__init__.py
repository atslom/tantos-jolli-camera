"""The Tantos Jolly integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .bridge import GlnkCameraBridge
from .const import CONF_CHANNEL, CONF_RTSP_PORT, DOMAIN

PLATFORMS = [Platform.CAMERA, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    bridge = GlnkCameraBridge(
        hass,
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        channel=int(entry.data[CONF_CHANNEL]),
        rtsp_port=entry.data[CONF_RTSP_PORT],
    )
    # Does not connect -- connecting takes over the panel's own controls, so
    # it only happens on demand via the capture switch entity (switch.py).
    await bridge.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = bridge

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        bridge: GlnkCameraBridge = hass.data[DOMAIN].pop(entry.entry_id)
        await bridge.async_stop()
    return unload_ok
