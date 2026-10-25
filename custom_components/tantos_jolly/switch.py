"""Switch platform for Tantos Jolly."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bridge import GlnkCameraBridge
from .const import CHANNEL_CHOICES, DOMAIN

SCAN_INTERVAL = timedelta(seconds=2)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    bridge: GlnkCameraBridge = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TantosJollyCaptureSwitch(entry, bridge)])


class TantosJollyCaptureSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Stream capture"
    _attr_icon = "mdi:cctv"
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry, bridge: GlnkCameraBridge) -> None:
        self._entry = entry
        self._bridge = bridge
        self._attr_unique_id = f"{entry.unique_id}-capture"
        host = entry.data[CONF_HOST]
        channel_label = CHANNEL_CHOICES.get(bridge.channel, "Unknown channel")
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{host}-{bridge.channel:#x}")},
            "name": f"Tantos Jolly – {channel_label} ({host})",
            "manufacturer": "Tantos",
            "model": "Jolly",
        }

    @property
    def is_on(self) -> bool:
        return self._bridge.capturing

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._bridge.start_capture()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._bridge.stop_capture()
        self.async_write_ha_state()