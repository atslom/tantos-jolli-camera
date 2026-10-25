# Tantos Jolli — Home Assistant integration

Живое видео с видеопанелей и камер **Tantos Jolli** (XiongMai/Sofia) по протоколу
GLNK прямо в Home Assistant — как `camera`-сущности. Настройка через UI (config
flow): IP хаба, GID устройства, логин/пароль. Никаких данных в коде не зашито.

## Установка (HACS)
1. HACS → Integrations → ⋮ → **Custom repositories** → добавьте URL этого репозитория,
   категория **Integration**.
2. Установите **Tantos Jolli**, перезапустите Home Assistant.
3. **Настройки → Устройства и службы → Добавить интеграцию → Tantos Jolli** и введите
   параметры хаба.

## Установка вручную
Скопируйте папку `custom_components/tantos_jolly/` в `/config/custom_components/`
на хосте Home Assistant и перезапустите HA.

> Домен интеграции — `tantos_jolly` (историческое написание в идентификаторе);
> устройство называется **Tantos Jolli**.
