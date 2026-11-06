# Tantos Jolli — Home Assistant integration (live video)

Русский: [README.ru.md](README.ru.md)

Live video from **Tantos Jolli** video-intercom panels and cameras (XiongMai/Sofia)
over the GLNK protocol, as Home Assistant `camera` entities. Configured entirely via
the UI (config flow): hub IP, device GID, username/password. Nothing is hardcoded —
all credentials are entered at setup and stored by Home Assistant.

## Install via HACS
1. Open **HACS → Integrations**.
2. Top-right **⋮ → Custom repositories**.
3. Repository: `https://github.com/atslom/tantos-jolli-camera`, Category: **Integration**. Click **Add**.
4. Find **Tantos Jolli** in the HACS list → **Download**.
5. **Restart Home Assistant**.
6. **Settings → Devices & Services → Add Integration → Tantos Jolli** and enter the hub parameters.

## Manual install
Copy the `custom_components/tantos_jolly/` folder into `/config/custom_components/`
on the Home Assistant host and restart HA.

> The integration domain is `tantos_jolly` (legacy identifier); the device is named
> **Tantos Jolli**. This is a HACS **integration** — the SD-recording archive is a
> separate **add-on** (github.com/atslom/tantos-ha-addons).
