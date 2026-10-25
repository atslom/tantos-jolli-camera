# Tantos Jolli — Home Assistant integration

[Русский](README.ru.md)

Live video from **Tantos Jolli** video-intercom panels and cameras (XiongMai/Sofia)
over the GLNK protocol, right in Home Assistant as `camera` entities. Configured
through the UI (config flow): hub IP, device GID, username/password. Nothing is
hardcoded — all credentials are entered at setup and stored by Home Assistant.

## Install (HACS)
1. HACS → Integrations → ⋮ → **Custom repositories** → add this repository URL,
   category **Integration**.
2. Install **Tantos Jolli** and restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Tantos Jolli** and enter the
   hub parameters.

## Manual install
Copy the `custom_components/tantos_jolly/` folder into `/config/custom_components/`
on the Home Assistant host and restart HA.

> The integration domain is `tantos_jolly` (legacy spelling in the identifier); the
> device is named **Tantos Jolli**.
