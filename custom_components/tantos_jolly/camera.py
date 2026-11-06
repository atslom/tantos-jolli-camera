"""Camera platform for Tantos Jolly."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bridge import GlnkCameraBridge
from .const import CHANNEL_CHOICES, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Short polling interval so HA re-reads `available` / `is_streaming` from
# the bridge's background thread promptly.
SCAN_INTERVAL = timedelta(seconds=2)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    bridge: GlnkCameraBridge = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TantosJollyCamera(entry, bridge)])


class TantosJollyCamera(Camera):
    """A single channel (call panel or camera) on a Tantos Jolly hub.

    Video is delivered via a local HLS playlist maintained by
    GlnkCameraBridge (ffmpeg writes m3u8+ts into a tmp dir, python3 -m
    http.server serves it on 127.0.0.1:<port>). This entity is a thin
    wrapper that just points HA's stream component at that playlist.
    """

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: ConfigEntry, bridge: GlnkCameraBridge) -> None:
        super().__init__()
        self._entry = entry
        self._bridge = bridge
        self._attr_unique_id = entry.unique_id
        host = entry.data[CONF_HOST]
        channel_label = CHANNEL_CHOICES.get(bridge.channel, "Unknown channel")
        self._attr_name = None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{host}-{bridge.channel:#x}")},
            "name": f"Tantos Jolly – {channel_label} ({host})",
            "manufacturer": "Tantos",
            "model": "Jolly",
        }

    @property
    def is_streaming(self) -> bool:
        return self._bridge.connected

    @property
    def available(self) -> bool:
        return self._bridge.connected

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "channel": f"{self._bridge.channel:#04x}",
            "capturing": self._bridge.capturing,
        }
        if self._bridge.last_error:
            attrs["last_error"] = self._bridge.last_error
        return attrs

    async def stream_source(self) -> str | None:
        """Return the HLS playlist URL that HA's stream component opens.

        Pointing at http://127.0.0.1:<port>/playlist.m3u8 lets HA's own
        stream_worker read HLS (which it has native support for) from a
        local python3 -m http.server, without any external RTSP/mediamtx
        dependency."""
        if not self._bridge.capturing:
            return None
        return self._bridge.hls_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Grab one JPEG frame for the dashboard card's thumbnail. Reads
        the first frame out of the HLS playlist via a short-lived ffmpeg."""
        if not self._bridge.capturing or not self._bridge.connected:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-loglevel", "error",
                "-probesize", "32",
                "-analyzeduration", "0",
                "-i", self._bridge.hls_url,
                "-frames:v", "1",
                "-f", "image2",
                "-c:v", "mjpeg",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return None
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        return stdout or None